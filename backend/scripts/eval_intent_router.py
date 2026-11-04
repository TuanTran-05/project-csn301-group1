"""Check the chat intent router against the real model, on real conversations.

The tests script the model's answers; this asks the configured provider
(AI_PROVIDER / AI_API_KEY / AI_ROUTER_MODEL from .env) to label each message
of a few conversations and prints where it disagrees with the expected label.
Nothing is written to the database and no device is contacted.

    cd backend
    .venv/bin/python scripts/eval_intent_router.py
"""

import sys

from network_copilot.ai import intent
from network_copilot.app import create_app

DEVICES = [{"hostname": "ROUTER-R1", "kind": "router"}, {"hostname": "SWITCH-SW1", "kind": "switch"}]

# (message, expected label, whether the assistant's previous reply was a design)
CONVERSATION = [
    ("hãy xây 1 hệ thống mạng gồm 1 router, 1 switch kết nối với router và 6 pc ket noi voi switch", "design", False),
    ("đặt dải ip là 172.20.5.0/24", "design", True),
    ("thêm 1 switch nữa và nối vào router", "design", True),
    ("chạy show running config trên sw1", "read", True),
    ("chạy show ip interface r1", "read", True),
    ("kiểm tra ospf trên router", "read", False),
    ("ping từ r1 tới 8.8.8.8", "read", False),
    ("tạo vlan 10 tên SALES trên sw1", "configure", False),
    ("cấu hình ip 10.0.0.1/30 cho cổng gi0/1 của r1", "configure", False),
    ("OSPF khác gì EIGRP?", "chat", False),
    ("cảm ơn bạn", "chat", True),
    ("vẽ lại sơ đồ với 2 router chạy dự phòng", "design", True),
]


def main() -> int:
    app = create_app()
    wrong = 0
    with app.app_context():
        provider = intent._provider()
        history = []
        for message, expected, after_design in CONVERSATION:
            context = {"devices": DEVICES, "conversation": history[-intent.HISTORY_TURNS:],
                       "last_reply_was_design": after_design}
            try:
                raw = provider.complete(intent.ROUTER_PROMPT, message, context, schema=intent.SCHEMA)
                got = intent.extract_json_object(raw).get("intent")
            except Exception as exc:  # report and carry on
                got = f"error: {type(exc).__name__}"
            mark = "OK " if got == expected else "SAI"
            wrong += got != expected
            print(f"{mark} {expected:9} -> {got:9} | {message}")
            history += [{"role": "user", "content": message},
                        {"role": "assistant", "content": "(đề xuất sơ đồ)" if expected == "design" else "(trả lời)"}]
    print(f"\n{len(CONVERSATION) - wrong}/{len(CONVERSATION)} đúng")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
