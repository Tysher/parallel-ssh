# This file is part of parallel-ssh.

# Copyright (C) 2014-2017 Panos Kittenis

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation, version 2.1.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

"""ssh2-python (libssh2) based SSH client package"""

import logging
import os
import pwd
# from socket import gaierror as sock_gaierror, error as sock_error
from socket import error as sock_error
from sys import version_info

from gevent import sleep, get_hub
from gevent import socket
from ssh2.error_codes import LIBSSH2_ERROR_EAGAIN
from ssh2.exceptions import AuthenticationError, AgentError, \
    SessionHandshakeError, SFTPHandleError, SFTPIOError as SFTPIOError_ssh2
from ssh2.session import Session
from ssh2.sftp import LIBSSH2_FXF_CREAT, LIBSSH2_FXF_WRITE, \
    LIBSSH2_FXF_TRUNC, LIBSSH2_SFTP_S_IRUSR, LIBSSH2_SFTP_S_IRGRP, \
    LIBSSH2_SFTP_S_IWUSR, LIBSSH2_SFTP_S_IXUSR, LIBSSH2_SFTP_S_IROTH, \
    LIBSSH2_SFTP_S_IXGRP, LIBSSH2_SFTP_S_IXOTH

from .exceptions import UnknownHostException, AuthenticationException, \
     ConnectionErrorException, SessionError, SFTPError, SFTPIOError
from .constants import DEFAULT_RETRIES
from .native.ssh2 import open_session, wait_select

host_logger = logging.getLogger('pssh.host_logger')
logger = logging.getLogger(__name__)
LINESEP = os.linesep.encode('utf-8') if version_info > (2,) else os.linesep
THREAD_POOL = get_hub().threadpool


class SSHClient(object):
    """ssh2-python based SSH client"""

    IDENTITIES = [
        os.path.expanduser('~/.ssh/id_rsa'),
        os.path.expanduser('~/.ssh/id_dsa'),
        os.path.expanduser('~/.ssh/identity')
    ]

    def __init__(self, host,
                 user=None, password=None, port=None,
                 pkey=None,
                 num_retries=DEFAULT_RETRIES,
                 allow_agent=True, timeout=None):
        # proxy_host=None, proxy_port=22, proxy_user=None,
        # proxy_password=None, proxy_pkey=None,
        self.host = host
        self.user = user if user else pwd.getpwuid(os.geteuid()).pw_name
        self.password = password
        self.port = port if port else 22
        self.pkey = pkey
        self.num_retries = num_retries
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.timeout = timeout * 1000 if timeout else None
        self.allow_agent = allow_agent
        self._connect()
        THREAD_POOL.apply(self._init)

    def _init(self):
        self.session = Session()
        if self.timeout:
            self.session.set_timeout(self.timeout)
        try:
            self.session.handshake(self.sock)
        except SessionHandshakeError as ex:
            msg = "Error connecting to host %s:%s - %s"
            raise SessionError(msg, self.host, self.port, ex)
        try:
            self.auth()
        except AuthenticationError as ex:
            msg = "Authentication error while connecting to %s:%s - %s"
            raise AuthenticationException(msg, self.host, self.port, ex)
        self.session.set_blocking(0)

    def _connect(self, retries=1):
        try:
            self.sock.connect((self.host, self.port))
        except sock_error as ex:
            logger.error("Error connecting to host '%s:%s' - retry %s/%s",
                         self.host, self.port, retries, self.num_retries)
            while retries < self.num_retries:
                sleep(5)
                return self._connect(retries=retries+1)
            error_type = ex.args[1] if len(ex.args) > 1 else ex.args[0]
            raise ConnectionErrorException(
                "Error connecting to host '%s:%s' - %s - retry %s/%s",
                self.host, self.port, str(error_type), retries,
                self.num_retries,)

    def _pkey_auth(self):
        pub_file = "%s.pub" % self.pkey
        logger.debug("Attempting authentication with public key %s for user %s",
                     pub_file, self.user)
        self._eagain(
            self.session.userauth_publickey_fromfile,
            self.user,
            pub_file,
            self.pkey,
            self.password if self.password is not None else '')

    def _identity_auth(self):
        for identity_file in self.IDENTITIES:
            if not os.path.isfile(identity_file):
                continue
            pub_file = "%s.pub" % (identity_file)
            logger.debug(
                "Trying to authenticate with identity file %s",
                identity_file)
            try:
                self._eagain(
                    self.session.userauth_publickey_fromfile,
                    self.user,
                    pub_file,
                    identity_file,
                    self.password if self.password is not None else '')
            except Exception:
                logger.debug("Authentication with identity file %s failed, "
                             "continuing with other identities",
                             identity_file)
                continue
            else:
                logger.debug("Authentication succeeded with identity file %s",
                             identity_file)
                return
        raise AuthenticationException("No authentication methods succeeded")

    def auth(self):
        if self.pkey is not None:
            logger.debug(
                "Proceeding with public key file authentication")
            return self._pkey_auth()
        if self.allow_agent:
            try:
                self.session.agent_auth(self.user)
            except (AuthenticationError, AgentError) as ex:
                logger.debug("Agent auth failed with %s, "
                             "continuing with other authentication methods",
                             ex)
            else:
                logger.debug("Authentication with SSH Agent succeeded")
                return
        try:
            self._identity_auth()
        except AuthenticationException:
            if self.password is None:
                raise
            logger.debug("Public key auth failed, trying password")
            self._password_auth()

    def _password_auth(self):
        if self._eagain(self.session.userauth_password,
                        self.user, self.password) != 0:
            raise AuthenticationException("Password authentication failed")

    def open_session(self):
        return open_session(self.sock, self.session)

    def execute(self, cmd, use_pty=False, channel=None):
        logger.debug("Opening new channel for execute")
        channel = self.open_session() if channel is None else channel
        if use_pty:
            self._eagain(channel.pty)
        logger.debug("Executing command '%s'" % cmd)
        self._eagain(channel.execute, cmd)
        return channel

    def read_stderr(self, channel):
        return self._read_output(channel, channel.read_stderr)

    def read_output(self, channel):
        return self._read_output(channel, channel.read)

    def _read_output(self, channel, read_func):
        remainder = b""
        _pos = 0
        _size, _data = read_func()
        while _size == LIBSSH2_ERROR_EAGAIN:
            logger.debug("Waiting on socket read")
            wait_select(self.sock, self.session)
            _size, _data = read_func()
        while _size > 0:
            logger.debug("Got data size %s", _size)
            while _pos < _size:
                linesep = _data[:_size].find(LINESEP, _pos)
                if linesep > 0:
                    if len(remainder) > 0:
                        yield remainder + _data[_pos:linesep].strip()
                        remainder = b""
                    else:
                        yield _data[_pos:linesep].strip()
                        _pos = linesep + 1
                else:
                    remainder += _data[_pos:]
                    break
            _size, _data = read_func()
            _pos = 0

    def wait_finished(self, channel):
        """Wait for EOF from channel, close channel and wait for
        close acknowledgement.

        :param channel: The channel to use
        :type channel: :py:class:`ssh2.channel.Channel`
        """
        if channel is None:
            return
        self._eagain(channel.wait_eof)
        self._eagain(channel.close)
        self._eagain(channel.wait_closed)

    def _eagain(self, func, *args, **kwargs):
        ret = func(*args, **kwargs)
        while ret == LIBSSH2_ERROR_EAGAIN:
            wait_select(self.sock, self.session)
            ret = func(*args, **kwargs)
        return ret

    def read_output_buffer(self, output_buffer, prefix=None,
                           callback=None,
                           callback_args=None,
                           encoding='utf-8'):
        """Read from output buffers and log to host_logger

        :param output_buffer: Iterator containing buffer
        :type output_buffer: iterator
        :param prefix: String to prefix log output to ``host_logger`` with
        :type prefix: str
        :param callback: Function to call back once buffer is depleted:
        :type callback: function
        :param callback_args: Arguments for call back function
        :type callback_args: tuple
        """
        prefix = '' if prefix is None else prefix
        for line in output_buffer:
            output = line.strip().decode(encoding)
            host_logger.info("[%s]%s\t%s", self.host, prefix, output)
            yield output
        if callback:
            callback(*callback_args)

    def run_command(self, command, sudo=False, user=None,
                    use_pty=False, shell=None,
                    encoding='utf-8'):
        # Fast path for no command substitution needed
        if not sudo and not user and not shell:
            _command = command
        else:
            _command = ''
            if sudo and not user:
                _command = 'sudo -S '
            elif user:
                _command = 'sudo -u %s -S ' % (user,)
            if shell:
                _command += '%s "%s"' % (shell, command,)
            else:
                _command += '$SHELL -c "%s"' % (command,)
        channel = self.execute(_command, use_pty=use_pty)
        return channel, self.host, \
            self.read_output_buffer(
                self.read_output(channel), encoding=encoding), \
            self.read_output_buffer(
                self.read_stderr(channel), encoding=encoding,
                prefix='\t[err]'), channel

    def _make_sftp(self):
        """Make SFTP client from open transport"""
        sftp = self.session.sftp_init()
        while sftp is None:
            wait_select(self.sock, self.session)
            sftp = self.session.sftp_init()
        return sftp

    def _mkdir(self, sftp, directory):
        """Make directory via SFTP channel

        :param sftp: SFTP client object
        :type sftp: :py:class:`paramiko.sftp_client.SFTPClient`
        :param directory: Remote directory to create
        :type directory: str

        Catches and logs at error level remote IOErrors on creating directory.
        """
        mode = LIBSSH2_SFTP_S_IRUSR | \
            LIBSSH2_SFTP_S_IWUSR | \
            LIBSSH2_SFTP_S_IXUSR | \
            LIBSSH2_SFTP_S_IRGRP | \
            LIBSSH2_SFTP_S_IROTH | \
            LIBSSH2_SFTP_S_IXGRP | \
            LIBSSH2_SFTP_S_IXOTH
        try:
            self._eagain(sftp.mkdir, directory, mode)
        except SFTPIOError_ssh2 as error:
            msg = "Error occured creating directory %s on host %s - %s"
            logger.error(msg, directory, self.host, error)
            raise SFTPIOError(msg, directory, self.host, error)
        logger.debug("Created remote directory %s", directory)

    def copy_file(self, local_file, remote_file, recurse=False,
                  sftp=None, _dir=None):
        sftp = self._make_sftp() if sftp is None else sftp
        if os.path.isdir(local_file) and recurse:
            return self._copy_dir(local_file, remote_file, sftp)
        elif os.path.isdir(local_file) and not recurse:
            raise ValueError("Recurse must be true if local_file is a "
                             "directory.")
        destination = self._remote_paths_split(remote_file)
        try:
            self._eagain(sftp.stat, destination)
        except SFTPHandleError:
            self.mkdir(sftp, destination)
        try:
            self.sftp_put(sftp, local_file, remote_file)
        except Exception as error:
            logger.error("Error occured copying file %s to remote destination "
                         "%s:%s - %s",
                         local_file, self.host, remote_file, error)
            raise error
        logger.info("Copied local file %s to remote destination %s:%s",
                    local_file, self.host, remote_file)

    def sftp_put(self, sftp, local_file, remote_file):
        mode = LIBSSH2_SFTP_S_IRUSR | \
               LIBSSH2_SFTP_S_IWUSR | \
               LIBSSH2_SFTP_S_IRGRP | \
               LIBSSH2_SFTP_S_IROTH
        f_flags = LIBSSH2_FXF_CREAT | LIBSSH2_FXF_WRITE | LIBSSH2_FXF_TRUNC
        try:
            with self._sftp_openfh(
                    sftp.open, remote_file, f_flags, mode) as remote_fh, \
                  open(local_file, 'rb') as local_fh:
                for data in local_fh:
                    remote_fh.write(data)
        except SFTPIOError_ssh2 as ex:
            msg = "Error writing to remote file %s - %s"
            logger.error(msg, remote_file, ex)
            raise SFTPIOError(msg, remote_file, ex)

    def mkdir(self, sftp, directory, parent_path=None):
        """Make directory via SFTP channel.

        Parent paths in the directory are created if they do not exist.

        :param sftp: SFTP client object
        :type sftp: :py:class:`paramiko.sftp_client.SFTPClient`
        :param directory: Remote directory to create
        :type directory: str

        Catches and logs at error level remote IOErrors on creating directory.
        """
        try:
            _parent_path, sub_dirs = directory.split('/', 1)
        except ValueError:
            _parent_path = directory.split('/', 1)[0]
            sub_dirs = None
        _directory = _parent_path if parent_path is None else \
            '/'.join([parent_path, _parent_path])
        try:
            self._eagain(sftp.stat, _directory)
        except SFTPHandleError:
            self._mkdir(sftp, _directory)
        if sub_dirs is not None:
            _parent_path = _parent_path if parent_path is None else \
                           '/'.join([parent_path, _parent_path])
            return self.mkdir(sftp, sub_dirs, parent_path=_parent_path)

    def _copy_dir(self, local_dir, remote_dir, sftp):
        """Call copy_file on every file in the specified directory, copying
        them to the specified remote directory."""
        file_list = os.listdir(local_dir)
        for file_name in file_list:
            local_path = os.path.join(local_dir, file_name)
            remote_path = '/'.join([remote_dir, file_name])
            self.copy_file(local_path, remote_path, recurse=True,
                           sftp=sftp)

    def copy_remote_file(self, remote_file, local_file, recurse=False,
                         sftp=None):
        """Copy remote file to local host via SFTP/SCP

        Copy is done natively using SFTP/SCP version 2, no scp command
        is used or required.

        :param remote_file: Remote filepath to copy from
        :type remote_file: str
        :param local_file: Local filepath where file(s) will be copied to
        :type local_file: str
        :param recurse: Whether or not to recursively copy directories
        :type recurse: bool

        :raises: :py:class:`ValueError` when a directory is supplied to
          ``local_file`` and ``recurse`` is not set
        :raises: :py:class:`pssh.exceptions.SFTPIOError` on I/O errors reading
          from SFTP
        :raises: :py:class:`OSError` on local OS errors like permission denied
        """
        sftp = self._make_sftp() if sftp is None else sftp
        try:
            self._eagain(sftp.stat, remote_file)
        except SFTPHandleError:
            msg = "Remote file or directory %s does not exist"
            logger.error(msg, remote_file)
            raise SFTPIOError(msg, remote_file)
        try:
            dir_h = self._sftp_openfh(sftp.opendir, remote_file)
        except SFTPError:
            pass
        else:
            if not recurse:
                raise ValueError("Recurse must be true if remote_file is a "
                                 "directory.")
            file_list = self._sftp_readdir(dir_h)
            return self._copy_remote_dir(file_list, remote_file,
                                         local_file, sftp)
        destination = os.path.join(os.path.sep, os.path.sep.join(
            [_dir for _dir in local_file.split('/')
             if _dir][:-1]))
        if not os.path.exists(destination):
            os.makedirs(destination)
        try:
            self.sftp_get(sftp, remote_file, local_file)
        except Exception as error:
            logger.error("Error occured copying file %s from remote destination"
                         " %s:%s - %s",
                         local_file, self.host, remote_file, error)
            raise
        logger.info("Copied local file %s from remote destination %s:%s",
                    local_file, self.host, remote_file)

    def _sftp_readdir(self, dir_h):
        for size, buf, attrs in dir_h.readdir():
            for line in buf.splitlines():
                yield line

    def _sftp_openfh(self, open_func, remote_file, *args):
        fh = open_func(remote_file, *args)
        errno = self.session.last_errno()
        while fh is None and errno == LIBSSH2_ERROR_EAGAIN:
            wait_select(self.sock, self.session, timeout=0.1)
            fh = open_func(remote_file, *args)
            errno = self.session.last_errno()
        if errno != LIBSSH2_ERROR_EAGAIN:
            msg = "Error opening file handle for file %s - error no: %s"
            logger.error(msg, remote_file, errno)
            raise SFTPError(msg, remote_file, errno)
        return fh

    def sftp_get(self, sftp, remote_file, local_file):
        try:
            with open(local_file, 'wb') as local_fh, \
                 self._sftp_openfh(sftp.open, remote_file, 0, 0) as remote_fh:
                for size, data in remote_fh:
                    local_fh.write(data)
        except SFTPIOError_ssh2 as ex:
            msg = "Error reading from remote file %s - %s"
            logger.error(msg, remote_file, ex)
            raise SFTPIOError(msg, remote_file, ex)

    def _copy_remote_dir(self, file_list, remote_dir, local_dir, sftp):
        for file_name in file_list:
            if file_name in ['.', '..']:
                continue
            remote_path = os.path.join(remote_dir, file_name)
            local_path = os.path.join(local_dir, file_name)
            self.copy_remote_file(remote_path, local_path, sftp=sftp,
                                  recurse=True)

    def _make_local_dir(self, dirpath):
        if os.path.exists(dirpath):
            return
        try:
            os.makedirs(dirpath)
        except OSError:
            logger.error("Unable to create local directory structure for "
                         "directory %s", dirpath)
            raise

    def _remote_paths_split(self, file_path):
        try:
            destination = '/'.join(
                [_dir for _dir in file_path.split('/')
                 if _dir][:-1])
        except IndexError:
            destination = ''
        return destination