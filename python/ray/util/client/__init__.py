from typing import List, Tuple, Dict, Any, Optional
from ray.job_config import JobConfig
from ray._private.client_mode_hook import (_explicitly_disable_client_mode,
                                           _explicitly_enable_client_mode)

import os
import sys
import logging
import json
import threading
import grpc

logger = logging.getLogger(__name__)

# This version string is incremented to indicate breaking changes in the
# protocol that require upgrading the client version.
CURRENT_PROTOCOL_VERSION = "2021-09-02"


class _ClientContext:
    def __init__(self):
        from ray.util.client.api import ClientAPI
        self.api = ClientAPI()
        self.client_worker: "ray.util.client.worker.Worker" = None
        # Makes client_id available after worker shuts down. Accessed from
        # self.__getattr__().
        self._client_id: str = None
        self._conn_info = {}
        self._server = None
        self._connected_with_init = False
        self._inside_client_test = False

    def connect(self,
                conn_str: str,
                job_config: JobConfig = None,
                secure: bool = False,
                metadata: List[Tuple[str, str]] = None,
                connection_retries: int = 3,
                namespace: str = None,
                *,
                ignore_version: bool = False,
                _credentials: Optional[grpc.ChannelCredentials] = None,
                ray_init_kwargs: Optional[Dict[str, Any]] = None
                ) -> Dict[str, Any]:
        """Connect the Ray Client to a server.

        Args:
            conn_str: Connection string, in the form "[host]:port"
            job_config: The job config of the server.
            secure: Whether to use a TLS secured gRPC channel
            metadata: gRPC metadata to send on connect
            connection_retries: number of connection attempts to make
            ignore_version: whether to ignore Python or Ray version mismatches.
                This should only be used for debugging purposes.

        Returns:
            Dictionary of connection info, e.g., {"num_clients": 1}.
        """
        # Delay imports until connect to avoid circular imports.
        from ray.util.client.worker import Worker
        if self.client_worker is not None:
            if self._connected_with_init:
                return
            raise Exception(
                "ray.init() called, but ray client is already connected")
        if not self._inside_client_test:
            # If we're calling a client connect specifically and we're not
            # currently in client mode, ensure we are.
            _explicitly_enable_client_mode()
        if namespace is not None:
            job_config = job_config or JobConfig()
            job_config.set_ray_namespace(namespace)
        if job_config is not None:
            runtime_env = json.loads(job_config.get_serialized_runtime_env())
            if runtime_env.get("pip") or runtime_env.get("conda"):
                logger.warning("The 'pip' or 'conda' field was specified in "
                               "the runtime env, so it may take some time to "
                               "install the environment before Ray connects.")
        try:
            self.client_worker = Worker(
                conn_str,
                secure=secure,
                _credentials=_credentials,
                metadata=metadata,
                connection_retries=connection_retries)
            self._client_id = self.client_worker._client_id
            self.api.worker = self.client_worker
            self.client_worker._server_init(job_config, ray_init_kwargs)
            self._conn_info = self.client_worker.connection_info()
            self._check_versions(self._conn_info, ignore_version)
            self._register_serializers()
            return self._conn_info
        except Exception:
            self.disconnect()
            raise

    def _register_serializers(self):
        """Register the custom serializer addons at the client side.

        The server side should have already registered the serializers via
        regular worker's serialization_context mechanism.
        """
        import ray.serialization_addons
        from ray.util.serialization import StandaloneSerializationContext
        ctx = StandaloneSerializationContext()
        ray.serialization_addons.apply(ctx)

    def _check_versions(self, conn_info: Dict[str, Any],
                        ignore_version: bool) -> None:
        local_major_minor = f"{sys.version_info[0]}.{sys.version_info[1]}"
        if not conn_info["python_version"].startswith(local_major_minor):
            version_str = f"{local_major_minor}.{sys.version_info[2]}"
            msg = "Python minor versions differ between client and server:" + \
                  f" client is {version_str}," + \
                  f" server is {conn_info['python_version']}"
            if ignore_version or "RAY_IGNORE_VERSION_MISMATCH" in os.environ:
                logger.warning(msg)
            else:
                raise RuntimeError(msg)
        if CURRENT_PROTOCOL_VERSION != conn_info["protocol_version"]:
            msg = "Client Ray installation incompatible with server:" + \
                  f" client is {CURRENT_PROTOCOL_VERSION}," + \
                  f" server is {conn_info['protocol_version']}"
            if ignore_version or "RAY_IGNORE_VERSION_MISMATCH" in os.environ:
                logger.warning(msg)
            else:
                raise RuntimeError(msg)

    def disconnect(self):
        """Disconnect the Ray Client.
        """
        if self.client_worker is not None:
            self.client_worker.close()
        self.client_worker = None

    # remote can be called outside of a connection, which is why it
    # exists on the same API layer as connect() itself.
    def remote(self, *args, **kwargs):
        """remote is the hook stub passed on to replace `ray.remote`.

        This sets up remote functions or actors, as the decorator,
        but does not execute them.

        Args:
            args: opaque arguments
            kwargs: opaque keyword arguments
        """
        return self.api.remote(*args, **kwargs)

    def __getattr__(self, key: str):
        if key == "id":
            return getattr(self, "_client_id")
        if key == "worker":
            return getattr(self, "client_worker")
        if self.is_connected():
            return getattr(self.api, key)
        elif key in ["is_initialized", "_internal_kv_initialized"]:
            # Client is not connected, thus Ray is not considered initialized.
            return lambda: False
        else:
            raise Exception("Ray Client is not connected. "
                            "Please connect by calling `ray.init`.")

    def is_connected(self) -> bool:
        if self.client_worker is None:
            return False
        return self.client_worker.is_connected()

    def init(self, *args, **kwargs):
        if self._server is not None:
            raise Exception("Trying to start two instances of ray via client")
        import ray.util.client.server.server as ray_client_server
        server_handle, address_info = ray_client_server.init_and_serve(
            "localhost:50051", *args, **kwargs)
        self._server = server_handle.grpc_server
        self.connect("localhost:50051")
        self._connected_with_init = True
        return address_info

    def shutdown(self, _exiting_interpreter=False):
        self.disconnect()
        import ray.util.client.server.server as ray_client_server
        if self._server is None:
            return
        ray_client_server.shutdown_with_server(self._server,
                                               _exiting_interpreter)
        self._server = None


# All connected context will be put here
# This struct will be guarded by a lock for thread safety
_all_contexts: Dict[str, _ClientContext] = {}
_lock = threading.Lock()

# This is the default context which is used when allow_multiple is not True
# It is also included in _all_contexts
_default_context = _ClientContext()


def num_connected_contexts():
    """Return the number of client connections active."""
    global _lock, _all_contexts
    with _lock:
        return len(_all_contexts)


class ManagedContext:
    """
    Basic context manager for a RayAPIStub.
    """
    dashboard_url: Optional[str]
    python_version: str
    ray_version: str
    ray_commit: str
    protocol_version: Optional[str]
    _num_clients: int
    _context_to_restore: Optional[_ClientContext]

    def __init__(self, info_dict: dict, context_to_restore: _ClientContext):
        self.dashboard_url = info_dict["dashboard_url"]
        self.python_version = info_dict["python_version"]
        self.ray_version = info_dict["ray_version"]
        self.ray_commit = info_dict["ray_commit"]
        self.protocol_version = info_dict["protocol_version"]
        self._num_clients = info_dict["num_clients"]
        self._context_to_restore = context_to_restore

    @property
    def id(self):
        """Current client's ID"""
        if self._context_to_restore is None:
            raise ValueError("Client context has not initialized or has "
                             "already disconnected")
        return self._context_to_restore.id

    def __enter__(self) -> "ManagedContext":
        self._swap_context()
        return self

    def __exit__(self, *exc) -> None:
        self._disconnect_with_context(False)
        self._swap_context()

    def disconnect(self) -> None:
        self._swap_context()
        self._disconnect_with_context(True)
        self._swap_context()

    def _swap_context(self) -> None:
        import ray.util.client as ray_client
        if self._context_to_restore is not None:
            self._context_to_restore = ray_client.ray.set_context(
                self._context_to_restore)

    def _disconnect_with_context(self, force_disconnect: bool) -> None:
        """
        Disconnect Ray. If it's a ray client and created with `allow_multiple`,
        it will do nothing. For other cases this either disconnects from the
        remote Client Server or shuts the current driver down.
        """
        import ray.util.client as ray_client
        import ray.util.client_connect as ray_client_connect
        if ray_client.ray.is_connected():
            if ray_client.ray.is_default() or force_disconnect:
                # This is the only client connection
                ray_client_connect.disconnect()
        elif ray.worker is None or ray.worker.global_worker.node is None:
            # Already disconnected.
            return
        elif ray.worker.global_worker.node.is_head():
            logger.debug(
                "The current Ray Cluster is scoped to this process. "
                "Disconnecting is not possible as it will shutdown the "
                "cluster.")
        else:
            # This is only a driver connected to an existing cluster.
            ray.shutdown()


def context_from_client_id(client_id: str) -> ManagedContext:
    """Return the client context with ID. Or raises KeyError if not found."""
    global _lock, _all_contexts
    with _lock:
        ctx = _all_contexts.get(client_id)
        if ctx is None:
            raise ValueError(
                f"Client {client_id} does not exist. "
                "It may have been shut down."
            )
        return ManagedContext(ctx._conn_info, ctx)


class RayAPIStub:
    """This class stands in as the replacement API for the `import ray` module.

    Much like the ray module, this mostly delegates the work to the
    _client_worker. As parts of the ray API are covered, they are piped through
    here or on the client worker API.
    """

    def __init__(self):
        self._cxt = threading.local()
        self._cxt.handler = _default_context
        self._inside_client_test = False

    def get_context(self):
        try:
            return self._cxt.__getattribute__("handler")
        except AttributeError:
            self._cxt.handler = _default_context
            return self._cxt.handler

    def set_context(self, cxt):
        old_cxt = self.get_context()
        if cxt is None:
            self._cxt.handler = _ClientContext()
        else:
            self._cxt.handler = cxt
        return old_cxt

    def is_default(self):
        return self.get_context() == _default_context

    def connect(self, *args, **kw_args):
        self.get_context()._inside_client_test = self._inside_client_test
        conn = self.get_context().connect(*args, **kw_args)
        global _lock, _all_contexts
        with _lock:
            _all_contexts[self._cxt.handler.id] = self._cxt.handler
        return conn

    def disconnect(self, *args, **kw_args):
        global _lock, _all_contexts, _default_context
        with _lock:
            if _default_context == self.get_context():
                for cxt in _all_contexts.values():
                    cxt.disconnect(*args, **kw_args)
                _all_contexts = {}
            else:
                self.get_context().disconnect(*args, **kw_args)
            _all_contexts.pop(self.get_context().id, None)
            if len(_all_contexts) == 0:
                _explicitly_disable_client_mode()

    def remote(self, *args, **kwargs):
        return self.get_context().remote(*args, **kwargs)

    def __getattr__(self, name):
        return self.get_context().__getattr__(name)

    def is_connected(self, *args, **kwargs):
        return self.get_context().is_connected(*args, **kwargs)

    def init(self, *args, **kwargs):
        ret = self.get_context().init(*args, **kwargs)
        global _lock, _all_contexts
        with _lock:
            _all_contexts[self._cxt.handler.id] = self._cxt.handler
        return ret

    def shutdown(self, *args, **kwargs):
        global _lock, _all_contexts
        with _lock:
            if _default_context == self.get_context():
                for cxt in _all_contexts.values():
                    cxt.shutdown(*args, **kwargs)
                _all_contexts = {}
            else:
                self.get_context().shutdown(*args, **kwargs)
            _all_contexts.pop(self.get_context().id, None)
            if len(_all_contexts) == 0:
                _explicitly_disable_client_mode()


ray = RayAPIStub()

# Someday we might add methods in this module so that someone who
# tries to `import ray_client as ray` -- as a module, instead of
# `from ray_client import ray` -- as the API stub
# still gets expected functionality. This is the way the ray package
# worked in the past.
#
# This really calls for PEP 562: https://www.python.org/dev/peps/pep-0562/
# But until Python 3.6 is EOL, here we are.
