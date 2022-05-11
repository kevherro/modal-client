from typing import NamedTuple, Optional

from modal_proto import api_pb2

from ._app_singleton import get_container_app
from ._app_state import AppState
from ._object_meta import ObjectMeta
from .exception import InvalidError


class ObjectLabel(NamedTuple):
    app_name: Optional[str] = None
    object_label: Optional[str] = None
    namespace: Optional[int] = None  # api_pb2.DEPLOYMENT_NAMESPACE


class Object(metaclass=ObjectMeta):
    """The shared base class of any synced/distributed object in Modal.

    Examples of objects include Modal primitives like Images and Functions, as
    well as distributed data structures like Queues or Dicts.
    """

    def __init__(self, app=None, label=None, object_id=None):
        self._label = label
        self._app = app
        self._object_id = object_id

    async def load(self, app, existing_object_id):
        raise NotImplementedError(f"Object factory of class {type(self)} has no load method")

    @classmethod
    def from_id(cls, object_id, app):
        parts = object_id.split("-")
        if len(parts) != 2:
            raise InvalidError(f"Object id {object_id} has no dash in it")
        prefix = parts[0]
        if prefix not in ObjectMeta.prefix_to_type:
            raise InvalidError(f"Object prefix {prefix} does not correspond to a type")
        object_cls = ObjectMeta.prefix_to_type[prefix]
        obj = Object.__new__(object_cls)
        Object.__init__(obj, app, object_id=object_id)
        return obj

    async def create(self, app=None):
        if app is None:
            app = get_container_app()
            if app is None:
                raise InvalidError(".create must be passed the app explicitly if not running in a container")
        if app.state != AppState.RUNNING:
            raise InvalidError(f"{self}.create(...): can only do this on a running app")
        object_id = await self.load(app, None)
        return Object.from_id(object_id, app)

    @property
    def object_id(self):
        return self._object_id

    @property
    def label(self):
        return self._label

    def get_creating_message(self) -> Optional[str]:
        return None

    def get_created_message(self) -> Optional[str]:
        return None

    @property
    def app(self):
        return self._app

    @classmethod
    def include(cls, app, app_name, object_label=None, namespace=api_pb2.DEPLOYMENT_NAMESPACE_ACCOUNT):
        """Use an object published with `modal.App.deploy`"""
        label = ObjectLabel(app_name, object_label, namespace)

        obj = Object.__new__(cls)
        Object.__init__(obj, app, label=label)
        return obj
