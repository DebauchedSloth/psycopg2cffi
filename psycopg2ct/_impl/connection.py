from functools import wraps
from collections import deque

from psycopg2ct._impl import encodings as _enc
from psycopg2ct._impl import exceptions
from psycopg2ct._impl import libpq
from psycopg2ct._impl.cursor import Cursor
from psycopg2ct._impl.xid import Xid


CONN_STATUS_SETUP = 0
CONN_STATUS_READY = 1
CONN_STATUS_BEGIN = 2
CONN_STATUS_PREPARED = 5

ISOLATION_LEVEL_AUTOCOMMIT = 0
ISOLATION_LEVEL_READ_UNCOMMITTED = 1
ISOLATION_LEVEL_READ_COMMITTED = 2
ISOLATION_LEVEL_REPEATABLE_READ = 3
ISOLATION_LEVEL_SERIALIZABLE = 4


def check_closed(func):
    @wraps(func)
    def check_closed_(self, *args, **kwargs):
        if self.closed:
            raise exceptions.InterfaceError('connection already closed')
        return func(self, *args, **kwargs)
    return check_closed_


def check_tpc(command):
    def decorator(func):
        @wraps(func)
        def check_tpc_(self, *args, **kwargs):
            if self._tpc_xid:
                raise exceptions.ProgrammingError(
                    '%s cannot be used during a two-phase transaction' % command)
            return func(self, *args, **kwargs)
        return check_tpc_
    return decorator


class Connection(object):

    # Various exceptions which should be accessible via the Connection
    # class according to dbapi 2.0
    Error = exceptions.Error
    DatabaseError = exceptions.DatabaseError
    IntegrityError = exceptions.IntegrityError
    InterfaceError = exceptions.InterfaceError
    InternalError = exceptions.InternalError
    NotSupportedError = exceptions.NotSupportedError
    OperationalError = exceptions.OperationalError
    ProgrammingError = exceptions.ProgrammingError
    Warning = exceptions.Warning


    def __init__(self, dsn):

        self.dsn = dsn
        self.status = CONN_STATUS_SETUP
        self._encoding = None

        self._closed = True
        self._cancel = None
        self._typecasts = {}
        self._tpc_xid = None
        self._notices = deque(maxlen=50)
        self._isolation_level = None

        # Connect
        self._pgconn = libpq.PQconnectdb(dsn)
        if not self._pgconn:
            raise exceptions.OperationalError('pgconnectdb() failed')
        elif libpq.PQstatus(self._pgconn) != libpq.CONNECTION_OK:
            error_msg = libpq.PQerrorMessage(self._pgconn)
            raise exceptions.OperationalError(error_msg)

        # Register notice processor
        self._notice_callback = libpq.PQnoticeProcessor(
            lambda arg, message: self._notices.append(message))
        libpq.PQsetNoticeProcessor(self._pgconn, self._notice_callback, None)

        # Setup the connection
        self._setup()

    def __del__(self):
        self._close()

    @check_closed
    def close(self):
        return self._close()

    @check_closed
    @check_tpc('rollback')
    def rollback(self):
        self._rollback()

    @check_closed
    @check_tpc('commit')
    def commit(self):
        self._commit()

    @check_closed
    def reset(self):
        self._setup()

    @property
    def isolation_level(self):
        return self._isolation_level

    @check_closed
    def set_isolation_level(self, level):
        if level < 0 or level > 4:
            raise ValueError('isolation level must be between 0 and 4')
        if self._isolation_level == level:
            return
        if self._isolation_level != ISOLATION_LEVEL_AUTOCOMMIT:
            self._rollback()
        self._isolation_level = level

    def set_session(self, isolation_level=None, readonly=None, deferrable=None,
                    autocommit=None):
        if isolation_level is not None:
            if isolation_level < 1 or isolation_level > 4:
                raise ValueError('isolation level must be between 1 and 4')
            if self._isolation_level == isolation_level:
                return
            if self._isolation_level != ISOLATION_LEVEL_AUTOCOMMIT:
                self._rollback()
            self._isolation_level = isolation_level

    @property
    def autocommit(self):
        raise NotImplementedError()

    @autocommit.setter
    def autocommit(self, value):
        raise NotImplementedError()

    @check_closed
    def get_backend_pid(self):
        return libpq.PQbackendPID(self._pgconn)

    def get_transaction_status(self):
        return libpq.PQtransactionStatus(self._pgconn)

    def cursor(self, name=None, cursor_factory=Cursor, withhold=False):
        cur = cursor_factory(self, name)
        if withhold:
            if name:
                cur.withhold = True
            else:
                raise exceptions.ProgrammingError(
                    "withhold=True can be specified only for named cursors")

        return cur

    @check_closed
    def cancel(self):
        errbuf = libpq.create_string_buffer(256)

        if libpq.PQcancel(self._cancel, errbuf, len(errbuf)) == 0:
            self._raise_operational_error(errbuf)

    @property
    def encoding(self):
        return self._encoding

    @check_closed
    def set_client_encoding(self, encoding):
        encoding = _enc.normalize(encoding)
        if self.encoding == encoding:
            return

        pyenc = _enc.encodings[encoding]
        self._rollback()
        self._execute_command('SET client_encoding = %s' % encoding)
        self._encoding = encoding
        self._py_enc = pyenc

    def get_exc_type_for_state(self, code):
        exc_type = None
        if code[0] == '2':
            if code[1] == '3':
                exc_type = exceptions.IntegrityError
        elif code[0] == '4':
            if code[1] == '2':
                exc_type = exceptions.ProgrammingError
        return exc_type

    @property
    def notices(self):
        return self._notices

    @property
    @check_closed
    def protocol_version(self):
        return libpq.PQprotocolVersion(self._pgconn)

    @property
    @check_closed
    def server_version(self):
        return libpq.PQserverVersion(self._pgconn)

    @property
    def closed(self):
        return self._closed

    @check_closed
    def xid(self, format_id, gtrid, bqual):
        return Xid(format_id, gtrid, bqual)

    @check_closed
    def tpc_begin(self, xid):
        if self.status != CONN_STATUS_READY:
            raise exceptions.ProgrammingError(
                'tpc_begin must be called outside a transaction')

        if self._isolation_level == ISOLATION_LEVEL_AUTOCOMMIT:
            raise exceptions.ProgrammingError(
                "tpc_begin can't be called in autocommit mode")

        self._begin_transaction()
        self._tpc_xid = xid

    @check_closed
    def tpc_commit(self):
        self._finish_tpc('COMMIT PREPARED', 'commit')

    @check_closed
    def tpc_rollback(self):
        self._finish_tpc('ROLLBACK PREPARED', 'abort')

    @check_closed
    def tpc_prepare(self):
        if not self._tpc_xid:
            raise exceptions.ProgrammingError(
                'prepare must be called inside a two-phase transaction')

    def _setup(self):
        pgres = libpq.PQexec(self._pgconn, 'SHOW default_transaction_isolation')
        if not pgres or libpq.PQresultStatus(pgres) != libpq.PGRES_TUPLES_OK:
            raise exceptions.OperationalError(
                "can't fetch default_isolation_level")

        isolation_level = libpq.PQgetvalue(pgres, 0, 0)
        libpq.PQclear(pgres)

        # Get current isolation level
        if (isolation_level == 'read uncommitted' or
            isolation_level == 'read committed'):
            self._isolation_level = ISOLATION_LEVEL_READ_COMMITTED
        else:
            self._isolation_level = ISOLATION_LEVEL_SERIALIZABLE

        # Get encoding
        client_encoding = libpq.PQparameterStatus(self._pgconn, 'client_encoding')
        self._encoding = _enc.normalize(client_encoding)
        self._py_enc = _enc.encodings[self.encoding]

        self._cancel = libpq.PQgetCancel(self._pgconn)
        if self._cancel is None:
            raise exceptions.OperationalError("can't get cancellation key")

        self._closed = False
        self.status = CONN_STATUS_READY

    def _begin_transaction(self):
        if (self.status == CONN_STATUS_READY and
            self._isolation_level != ISOLATION_LEVEL_AUTOCOMMIT):
            sql = [
                    None,
                    'BEGIN; SET TRANSACTION ISOLATION LEVEL READ COMMITTED',
                    'BEGIN; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE',
                ][self._isolation_level]

            self._execute_command(sql)
            self.status = CONN_STATUS_BEGIN

    def _execute_command(self, command):
        pgres = libpq.PQexec(self._pgconn, command)
        if not pgres:
            self._raise_operational_error(None)
        try:
            pgstatus = libpq.PQresultStatus(pgres)
            if pgstatus != libpq.PGRES_COMMAND_OK:
                self._raise_operational_error(pgres)
        finally:
            libpq.PQclear(pgres)

    def _execute_tpc_command(self, command):
        from psycopg2ct import QuotedString

        tid = self._tpc_xid.as_tid()
        tid = QuotedString(tid)
        tid.prepare(self)
        tid = str(tid.quote())
        cmd = '%s %s;' % (command, tid)
        self._execute_command(cmd)

    def _finish_tpc(self, command, fallback):

        if not self._tpc_xid:
            raise exceptions.ProgrammingError(
                'tpc_commit/tpc_rollback with no parameter must be '
                'called in a two-phase transaction')

        if self.status == CONN_STATUS_BEGIN:
            if fallback == 'commit':
                self._commit()
            elif fallback == 'abort':
                self._rollback()
            else:
                raise exceptions.InternalError(
                    'bad fallback passed to finish_tpc')
        elif self.status == CONN_STATUS_PREPARED:
            self._execute_tpc_command(command)
        else:
            raise exceptions.InterfaceError(
                'unexpected state in tpc_commit/tpc_rollback')

        self.status = CONN_STATUS_READY
        self._tpc_xid = None

    def _close(self):
        self._closed = True

        if self._pgconn:
            libpq.PQfinish(self._pgconn)
            self._pgconn = None
        self._notices = None

    def _commit(self):
        if (self._isolation_level == ISOLATION_LEVEL_AUTOCOMMIT or
            self.status != CONN_STATUS_BEGIN):
            return
        self._execute_command('COMMIT')
        self.status = CONN_STATUS_READY

    def _rollback(self):
        if (self._isolation_level == ISOLATION_LEVEL_AUTOCOMMIT or
            self.status != CONN_STATUS_BEGIN):
            return
        self._execute_command('ROLLBACK')
        self.status = CONN_STATUS_READY

    def _raise_operational_error(self, pgres):
        code = None
        error = None
        if pgres:
            error = libpq.PQresultErrorMessage(pgres)
            if error is not None:
                code = libpq.PQresultErrorField(pgres, libpq.PG_DIAG_SQLSTATE)
        if error is None:
            error = libpq.PQerrorMessage(self._pgconn)
        exc_type = None
        if code is not None:
            exc_type = self.get_exc_type_for_state(code)
        if exc_type is None:
            exc_type = exceptions.OperationalError
        raise exc_type(error)


def connect(dsn=None, database=None, host=None, port=None, user=None,
            password=None, async=False, connection_factory=Connection):
    if async:
        raise NotImplementedError()

    if dsn is None:
        args = []
        if database is not None:
            args.append('dbname=%s' % database)
        if host is not None:
            args.append('host=%s' % host)
        if port is not None:
            if isinstance(port, str):
                port = int(port)

            if not isinstance(port, int):
                raise TypeError('port must be a string or int')
            args.append('port=%d' % port)
        if user is not None:
            args.append('user=%s' % user)
        if password is not None:
            args.append('password=%s' % password)
        dsn = ' '.join(args)
    return connection_factory(dsn)
