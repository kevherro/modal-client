# Copyright Modal Labs 2022
import os
from typing import Dict, Optional, Union

from grpclib import GRPCError, Status

from modal._types import typechecked
from modal_proto import api_pb2
from modal_utils.async_utils import synchronize_api

from ._resolver import Resolver
from .exception import InvalidError
from .object import _StatefulObject

ENV_DICT_WRONG_TYPE_ERR = "the env_dict argument to Secret has to be a dict[str, Union[str, None]]"


class _Secret(_StatefulObject, type_prefix="st"):
    """Secrets provide a dictionary of environment variables for images.

    Secrets are a secure way to add credentials and other sensitive information
    to the containers your functions run in. You can create and edit secrets on
    [the dashboard](/secrets), or programmatically from Python code.

    See [the secrets guide page](/docs/guide/secrets) for more information.
    """

    @typechecked
    @staticmethod
    def from_dict(
        env_dict: Dict[
            str, Union[str, None]
        ] = {},  # dict of entries to be inserted as environment variables in functions using the secret
    ):
        """Create a secret from a str-str dictionary. Values can also be `None`, which is ignored.

        Usage:
        ```python
        @stub.function(secret=modal.Secret.from_dict({"FOO": "bar"})
        def run():
            print(os.environ["FOO"])
        ```
        """
        if not isinstance(env_dict, dict):
            raise InvalidError(ENV_DICT_WRONG_TYPE_ERR)

        env_dict_filtered: dict[str, str] = {k: v for k, v in env_dict.items() if v is not None}
        if not all(isinstance(k, str) for k in env_dict_filtered.keys()):
            raise InvalidError(ENV_DICT_WRONG_TYPE_ERR)
        if not all(isinstance(v, str) for v in env_dict_filtered.values()):
            raise InvalidError(ENV_DICT_WRONG_TYPE_ERR)

        async def _load(provider: _Secret, resolver: Resolver, existing_object_id: Optional[str]):
            req = api_pb2.SecretCreateRequest(
                app_id=resolver.app_id,
                env_dict=env_dict_filtered,
                existing_secret_id=existing_object_id,
            )
            try:
                resp = await resolver.client.stub.SecretCreate(req)
            except GRPCError as exc:
                if exc.status == Status.INVALID_ARGUMENT:
                    raise InvalidError(exc.message)
                if exc.status == Status.FAILED_PRECONDITION:
                    raise InvalidError(exc.message)
                raise
            provider._hydrate(resp.secret_id, resolver.client, None)

        rep = f"Secret.from_dict([{', '.join(env_dict.keys())}])"
        return _Secret._from_loader(_load, rep)

    @staticmethod
    def from_dotenv(path=None):
        """Create secrets from a .env file automatically.

        If no argument is provided, it will use the current working directory as the starting
        point for finding a `.env` file. Note that it does not use the location of the module
        calling `Secret.from_dotenv`.

        If called with an argument, it will use that as a starting point for finding `.env` files.
        In particular, you can call it like this:
        ```python
        @stub.function(secret=modal.Secret.from_dotenv(__file__))
        def run():
            print(os.environ["USERNAME"])  # Assumes USERNAME is defined in your .env file
        ```

        This will use the location of the script calling `modal.Secret.from_dotenv` as a
        starting point for finding the `.env` file.
        """

        async def _load(provider: _Secret, resolver: Resolver, existing_object_id: Optional[str]):
            try:
                from dotenv import dotenv_values, find_dotenv
                from dotenv.main import _walk_to_root
            except ImportError:
                raise ImportError(
                    "Need the `dotenv` package installed. You can install it by running `pip install python-dotenv`."
                )

            if path is not None:
                # This basically implements the logic in find_dotenv
                for dirname in _walk_to_root(path):
                    check_path = os.path.join(dirname, ".env")
                    if os.path.isfile(check_path):
                        dotenv_path = check_path
                        break
                else:
                    dotenv_path = ""
            else:
                # TODO(erikbern): dotenv tries to locate .env files based on the location of the file in the stack frame.
                # Since the modal code "intermediates" this, a .env file in the user's local directory won't be picked up.
                # To simplify this, we just support the cwd and don't do any automatic path inference.
                dotenv_path = find_dotenv(usecwd=True)

            env_dict = dotenv_values(dotenv_path)

            req = api_pb2.SecretCreateRequest(
                app_id=resolver.app_id,
                env_dict=env_dict,
                existing_secret_id=existing_object_id,
            )
            resp = await resolver.client.stub.SecretCreate(req)

            provider._hydrate(resp.secret_id, resolver.client, None)

        return _Secret._from_loader(_load, "Secret.from_dotenv()")


Secret = synchronize_api(_Secret)
