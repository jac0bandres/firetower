"""Accuracy eval against tests/messages.json: a list of {"text", "node_id",
"expected_urgency"} labeled by a field tester. Reports recall with and
without the keyword net, and states the sample size."""

import json
import os

import triage

MESSAGES_PATH = os.path.join(os.path.dirname(__file__), "messages.json")


def load_messages() -> list[dict]:
    with open(MESSAGES_PATH, encoding="utf-8") as f:
        return json.load(f)


def score(messages: list[dict], use_keyword_net: bool) -> dict:
    raise NotImplementedError


def main():
    messages = load_messages()
    if not messages:
        print("tests/messages.json is empty — add labeled messages before running eval.")
        return
    print(f"Sample size: {len(messages)}")
    print("With keyword net: ", score(messages, use_keyword_net=True))
    print("Without keyword net:", score(messages, use_keyword_net=False))


if __name__ == "__main__":
    main()
