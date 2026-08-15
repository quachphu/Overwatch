"""One-shot: create the Band room, add all five registered agents, and post the message that
starts the first real handoff. Uses each agent's own REST credentials — the account-level
BAND_API_KEY's `human_api_*` surface returned a live 403 (`plan_required`, Enterprise-only), so
room/participant/message management goes through the free `agent_api_*` surface instead
(RESEARCH.md §13.20).
"""

import asyncio
import sys

from band.client.rest import AsyncRestClient
from band.config import load_agent_config
from band_rest.types.chat_message_request import ChatMessageRequest
from band_rest.types.chat_message_request_mentions_item import ChatMessageRequestMentionsItem
from band_rest.types.chat_room_request import ChatRoomRequest
from band_rest.types.participant_request import ParticipantRequest

CONFIG_PATH = "agent_config.yaml"
AGENT_KEYS = ["scout", "triage", "recruiter", "bursar", "critic"]


async def main() -> None:
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://the-internet.herokuapp.com/status_codes"

    ids = {}
    for key in AGENT_KEYS:
        agent_id, api_key = load_agent_config(key, config_path=CONFIG_PATH)
        ids[key] = (agent_id, api_key)

    scout_id, scout_key = ids["scout"]
    bursar_id, bursar_key = ids["bursar"]
    scout_client = AsyncRestClient(api_key=scout_key)
    bursar_client = AsyncRestClient(api_key=bursar_key)

    room = await scout_client.agent_api_chats.create_agent_chat(
        chat=ChatRoomRequest(title="Overwatch — live crew demo")
    )
    chat_id = room.data.id
    print(f"created room: {chat_id}")

    for key in AGENT_KEYS:
        agent_id, _ = ids[key]
        try:
            await scout_client.agent_api_participants.add_agent_chat_participant(
                chat_id=chat_id,
                participant=ParticipantRequest(participant_id=agent_id, role="member"),
            )
            print(f"added participant: {key} ({agent_id})")
        except Exception as exc:
            print(f"add participant {key} failed (may already be in room): {exc}")

    # An agent cannot @mention itself (live 422 `cannot_mention_self`), and this also matches
    # the real flow: docs/AGENTS.md has Bursar @Scout on a paid order, not Scout self-starting.
    msg = await bursar_client.agent_api_messages.create_agent_chat_message(
        chat_id=chat_id,
        message=ChatMessageRequest(
            content=f"Paid order confirmed. @Scout scan {target_url}",
            mentions=[ChatMessageRequestMentionsItem(id=scout_id, name="Scout", handle="scout")],
        ),
    )
    print(f"posted kickoff message: {msg}")
    print(f"CHAT_ID={chat_id}")


if __name__ == "__main__":
    asyncio.run(main())
