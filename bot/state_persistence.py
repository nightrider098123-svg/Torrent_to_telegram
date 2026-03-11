# // State persistence for serializing and deserializing minimal state (queue, active downloads) to the ADMIN_CHANNEL_ID.
import json
import logging
from typing import Dict, Any, List
import asyncio

logger = logging.getLogger(__name__)

class StatePersistence:
    def __init__(self, client, admin_channel_id: int):
        self.client = client
        self.admin_channel_id = admin_channel_id
        self._last_msg_id = None
        self._state_cache: Dict[str, Any] = {"queued_magnets": [], "uploading": [], "uploaded": []}

    async def load_state(self) -> Dict[str, Any]:
        """Loads state from the latest admin channel message containing state JSON."""
        try:
            # We search backwards for our state message
            async for message in self.client.get_chat_history(self.admin_channel_id, limit=50):
                if message.text and message.text.startswith("STATE_MANIFEST:"):
                    raw_json = message.text.replace("STATE_MANIFEST:", "", 1).strip()
                    try:
                        data = json.loads(raw_json)
                        self._last_msg_id = message.id
                        self._state_cache = data
                        logger.info("Successfully rehydrated state from ADMIN_CHANNEL_ID.")
                        return data
                    except json.JSONDecodeError:
                        logger.error("Failed to decode STATE_MANIFEST JSON.")
            return self._state_cache
        except Exception as e:
            logger.error(f"Failed to fetch state from Telegram: {e}")
            return self._state_cache

    async def save_state(self, state_dict: Dict[str, Any]):
        """Serializes and sends a state manifest to the admin channel."""
        self._state_cache.update(state_dict)
        manifest_text = f"STATE_MANIFEST:\n{json.dumps(self._state_cache, indent=2)}"

        try:
            if self._last_msg_id:
                # Try to edit the existing message
                try:
                    await self.client.edit_message_text(
                        chat_id=self.admin_channel_id,
                        message_id=self._last_msg_id,
                        text=manifest_text
                    )
                    return
                except Exception as e:
                    logger.warning(f"Failed to edit previous manifest message, sending new one: {e}")

            # If no previous message or editing failed, send a new one
            msg = await self.client.send_message(
                chat_id=self.admin_channel_id,
                text=manifest_text
            )
            self._last_msg_id = msg.id
        except Exception as e:
            logger.error(f"Failed to save state to Telegram: {e}")

    async def update_uploading(self, file_path: str):
        if file_path not in self._state_cache["uploading"]:
            self._state_cache["uploading"].append(file_path)
            await self.save_state({})

    async def mark_uploaded(self, file_path: str):
        if file_path in self._state_cache["uploading"]:
            self._state_cache["uploading"].remove(file_path)
        if file_path not in self._state_cache["uploaded"]:
            self._state_cache["uploaded"].append(file_path)
        await self.save_state({})
