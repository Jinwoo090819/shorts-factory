import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "data" / "scripts" / "2026-09-19.json"
BUFFER_ENDPOINT = "https://api.buffer.com"


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value


def graphql(query: str, variables=None):
    token = required("BUFFER_API_KEY")
    response = requests.post(
        BUFFER_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {payload['errors']}")
    return payload.get("data") or {}


def main():
    item = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))
    post_id = str(item.get("buffer_post_id") or "").strip()
    if not post_id:
        raise RuntimeError("No buffer_post_id found")

    mutation = """
    mutation EditPost($input: EditPostInput!) {
      editPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            status
            dueAt
            externalLink
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """
    data = graphql(mutation, {"input": {"id": post_id, "mode": "shareNow"}})
    result = data.get("editPost") or {}
    if result.get("message"):
        raise RuntimeError(f"Buffer rejected shareNow: {result['message']}")
    post = result.get("post") or {}
    if not post.get("id"):
        raise RuntimeError(f"Buffer returned no post id: {result}")
    print(json.dumps({"ok": True, "post": post}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
