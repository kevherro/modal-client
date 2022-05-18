import platform
import pytest
import time

from modal._container_entrypoint import main

# from modal._test_support import SLEEP_DELAY
from modal._serialization import serialize
from modal.client import Client
from modal_proto import api_pb2

EXTRA_TOLERANCE_DELAY = 0.25
FUNCTION_CALL_ID = "fc-123"
SLEEP_DELAY = 0.1

skip_non_linux = pytest.mark.skipif(
    platform.system() != "Linux", reason="sleep is inaccurate on Github Actions runners."
)


def _get_inputs(client):
    args = ((42,), {})
    function_input = api_pb2.FunctionInput(args=serialize(args), function_call_id=FUNCTION_CALL_ID)

    return [
        api_pb2.FunctionGetInputsResponse(inputs=[function_input]),
        api_pb2.FunctionGetInputsResponse(inputs=[api_pb2.FunctionInput(kill_switch=True)]),
    ]


def _get_output(function_output_req: api_pb2.FunctionPutOutputsRequest) -> api_pb2.GenericResult:
    assert len(function_output_req.outputs) == 1
    return function_output_req.outputs[0]


def _run_container(servicer, module_name, function_name):
    with Client(servicer.remote_addr, api_pb2.CLIENT_TYPE_CONTAINER, ("ta-123", "task-secret")) as client:
        servicer.container_inputs = _get_inputs(client)

        function_def = api_pb2.Function(
            module_name=module_name,
            function_name=function_name,
        )

        # Note that main is a synchronous function, so we need to run it in a separate thread
        container_args = api_pb2.ContainerArguments(
            task_id="ta-123",
            function_id="fu-123",
            app_id="se-123",
            function_def=function_def,
        )

        servicer.object_ids = {
            "image": "im-1",
            "modal._test_support.square": "fu-2",
            "modal._test_support.square_sync_returning_async": "fu-3",
            "modal._test_support.square_async": "fu-4",
            "modal._test_support.raises": "fu-5",
        }
        main(container_args, client)

        return client, servicer.container_outputs


def test_container_entrypoint_success(servicer, reset_global_apps, event_loop):
    t0 = time.time()
    client, outputs = _run_container(servicer, "modal._test_support", "square")
    assert 0 <= time.time() - t0 < EXTRA_TOLERANCE_DELAY

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**2)


@skip_non_linux
def test_container_entrypoint_async(servicer, reset_global_apps):
    t0 = time.time()
    client, outputs = _run_container(servicer, "modal._test_support", "square_async")
    assert SLEEP_DELAY <= time.time() - t0 < SLEEP_DELAY + EXTRA_TOLERANCE_DELAY

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**2)


@skip_non_linux
def test_container_entrypoint_sync_returning_async(servicer, reset_global_apps):
    t0 = time.time()
    client, outputs = _run_container(servicer, "modal._test_support", "square_sync_returning_async")
    assert SLEEP_DELAY <= time.time() - t0 < SLEEP_DELAY + EXTRA_TOLERANCE_DELAY

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_SUCCESS
    assert output.data == serialize(42**2)


@skip_non_linux
def test_container_entrypoint_failure(servicer, reset_global_apps):
    client, outputs = _run_container(servicer, "modal._test_support", "raises")

    assert len(outputs) == 1
    assert isinstance(outputs[0], api_pb2.FunctionPutOutputsRequest)

    output = _get_output(outputs[0])
    assert output.status == api_pb2.GenericResult.GENERIC_STATUS_FAILURE
    assert output.exception == "Exception('Failure!')"
    assert "Traceback" in output.traceback
