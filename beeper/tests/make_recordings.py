"""Regenerate explicitly invented recordings, without source network access."""

import json
from pathlib import Path

NETWORKS = ["discord", "instagram", "sms", "reddit", "snapchat"]
MESSAGE_COLUMNS = "id platform conversation_id native_id sender_id sender_name direction sent_at time_raw time_precision body edited_at reply_to attachments_json reactions_json metadata_json identity_quality first_imported_at last_seen_at".split()


def generate():
    archive = dict(messages=[], conversations=[], identities=[], message_aliases=[])
    live = {"responses": []}
    accounts, chats = [], []

    def response(path, body, params=None):
        live["responses"].append(dict(path=path, params=params or {}, body=body))

    for net in NETWORKS:
        aid, cid = "fake-account-" + net, "fake-chat-" + net
        accounts.append(
            dict(
                accountID=aid,
                network=net.title(),
                status="connected",
                user={"id": "fake-owner"},
            )
        )
        chat = dict(
            id=cid,
            accountID=aid,
            network=net,
            title="Invented " + net + " conversation",
            type="single",
        )
        chats.append(chat)
        archive["conversations"].append(
            dict(
                id=cid,
                platform=net,
                native_id="fake-conversation",
                title=chat["title"],
                kind="dm",
                guild_id="",
                metadata_json="{}",
            )
        )
        archive["identities"].append(
            dict(
                identity_id="fake-identity-" + net,
                platform=net,
                namespace="phone" if net == "sms" else "user_id",
                native_id="fake-sender",
            )
        )
        messages = []
        for index in range(6):
            row = dict.fromkeys(MESSAGE_COLUMNS, "")
            row.update(
                id=f"{net}:fake-{index}",
                platform=net,
                conversation_id=cid,
                native_id="fake-duplicated" if index > 1 else "",
                sender_id="fake-sender",
                sender_name="Invented Sender",
                direction=["incoming", "outgoing", "unknown"][index % 3],
                sent_at=f"2020-01-0{6 - index}T12:00:00+02:00" if index != 2 else "",
                time_precision="exact",
                body="Invented orchard " + str(index) if index % 2 else "",
                attachments_json='[{"fileName":"fake-photo.jpg","type":"img"}]'
                if index % 2 == 0
                else "[]",
                reactions_json=json.dumps(
                    [
                        {
                            "emoji": "+1",
                            "sender_id": "fake-sender",
                            "sender_name": "Invented Sender",
                        }
                    ]
                )
                if index == 3
                else "[]",
                metadata_json='{"invented":true}',
                identity_quality="native",
                first_imported_at="2020-02-01T00:00:00Z",
                last_seen_at="2020-02-01T00:00:00Z",
                reply_to=f"{net}:fake-5" if index == 0 else "",
            )
            archive["messages"].append(row)
            messages.append(
                dict(
                    id=f"fake-message-{index}",
                    accountID=aid,
                    chatID=cid,
                    senderID="fake-beeper-sender",
                    senderName="Invented Sender",
                    timestamp=f"2020-01-0{6 - index}T12:00:00+02:00",
                    sortKey=f"fake-sort-{index}",
                    text="Invented live orchard " + str(index),
                    isSender=index % 2 == 0,
                    linkedMessageID="fake-message-5" if index == 0 else None,
                    **(
                        {
                            "reactions": [
                                {
                                    "id": "fake-reaction",
                                    "participantID": "fake-beeper-sender",
                                    "reactionKey": "+1",
                                }
                            ]
                        }
                        if index == 3
                        else {}
                    ),
                )
            )

        def page(items, more, newest, oldest):
            return dict(
                items=items, hasMore=more, newestCursor=newest, oldestCursor=oldest
            )

        path = "v1/chats/" + cid + "/messages"
        response(path, page(messages[:2], True, "fake-head", "fake-page-1"))
        response(
            path,
            page(messages[2:4], True, "fake-page-1", "fake-page-2"),
            dict(cursor="fake-page-1", direction="before"),
        )
        response(
            path,
            page(messages[4:], False, "fake-page-2", "fake-end"),
            dict(cursor="fake-page-2", direction="before"),
        )
        response(
            path,
            page([], False, "fake-head", "fake-head"),
            dict(cursor="fake-head", direction="after"),
        )
        response(
            path,
            page([], False, "fake-end", "fake-end"),
            dict(cursor="fake-end", direction="before"),
        )
    response("v1/accounts", accounts)
    response(
        "v1/chats",
        dict(
            items=chats[:2],
            hasMore=True,
            newestCursor="fake-chats-head",
            oldestCursor="fake-chats-next",
        ),
        {"limit": 200},
    )
    response(
        "v1/chats",
        dict(
            items=chats[2:],
            hasMore=False,
            newestCursor="fake-chats-next",
            oldestCursor="fake-chats-end",
        ),
        dict(limit=200, cursor="fake-chats-next", direction="before"),
    )
    root = Path(__file__).parent / "recorded"
    root.mkdir(exist_ok=True)
    for name, data in [("archive", archive), ("live", live)]:
        (root / (name + ".json")).write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    generate()
