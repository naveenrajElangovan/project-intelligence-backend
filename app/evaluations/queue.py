import json

from app.config import Settings


class EvaluationQueue:
    """Managed-identity Azure Service Bus publisher; no Phoenix credentials here."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, payload: dict[str, object]) -> None:
        # Keep the optional queue SDK off application import/startup. A missing
        # deployment dependency can fail evaluation submission, but can never
        # prevent the user-facing API or answer path from starting.
        from azure.identity.aio import DefaultAzureCredential
        from azure.servicebus import ServiceBusMessage
        from azure.servicebus.aio import ServiceBusClient

        credential = DefaultAzureCredential(
            managed_identity_client_id=(
                self._settings.evaluation_managed_identity_client_id or None
            )
        )
        client = ServiceBusClient(
            fully_qualified_namespace=self._settings.evaluation_service_bus_namespace,
            credential=credential,
        )
        try:
            async with client:
                sender = client.get_queue_sender(
                    queue_name=self._settings.evaluation_service_bus_queue
                )
                async with sender:
                    await sender.send_messages(
                        ServiceBusMessage(
                            json.dumps(payload, separators=(",", ":")),
                            content_type="application/json",
                            application_properties={
                                "run_id": str(payload["run_id"]),
                                "project_id": str(payload["project_id"]),
                                "schema_version": "1",
                            },
                        )
                    )
        finally:
            await credential.close()
