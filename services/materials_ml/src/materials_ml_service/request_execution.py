"""Keep request cancellation attached to its computation until process cleanup completes."""
import asyncio
import anyio

from .prediction_application import CallCancellation


async def signal_prediction_cancel(service, call):
    call.event.set()
    if call.owner:
        with anyio.CancelScope(shield=True):
            try:
                await anyio.to_thread.run_sync(lambda: service.request_prediction_cancel(*call.owner))
            except Exception:
                pass  # Event still stops computation; final cleanup/restart retries durable state reconciliation.


async def execute_prediction(service, scope, key, body, cancellation=None, *, expected_digest=None):
    call = cancellation or CallCancellation()
    async def work():
        return await anyio.to_thread.run_sync(lambda: service.predict_model(
            scope, key, body.model_id, body.input_dataset_id, call,
            **({"expected_digest": expected_digest} if expected_digest is not None else {})), abandon_on_cancel=False)
    task = asyncio.create_task(work())
    try:
        return await asyncio.shield(task)
    except BaseException:
        await signal_prediction_cancel(service, call)
        with anyio.CancelScope(shield=True):
            # asyncio task cancellation does not terminate a Python thread or native estimator.
            try:
                await asyncio.shield(task)
            except Exception:
                pass
        raise
    finally:
        if call.event.is_set() and call.owner:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda: service.cancel_prediction(*call.owner))


async def resource_prediction(service, scope, key, body, request):
    call = CallCancellation()
    failure = None
    async with anyio.create_task_group() as group:
        async def disconnect():
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    await signal_prediction_cancel(service, call)
                    return
        group.start_soon(disconnect)
        try:
            result = await execute_prediction(service, scope, key, body, call)
        except Exception as error:
            failure = error
        finally:
            group.cancel_scope.cancel()
    if failure:
        raise failure
    return result
