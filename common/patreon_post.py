"""
Patreon has no working post-creation API (the endpoint existed once but was
removed — see https://www.patreondevelopers.com/t/post-via-the-api/9827,
confirmed against a live account: every call 404s regardless of token).
So instead of posting, this just formats a ready-to-paste post so publishing
to Patreon is copy-paste instead of writing it from scratch each time.
"""


def format_patreon_post(title: str, description: str, youtube_url: str) -> str:
    return f"{title}\n\n{description}\n\n{youtube_url}"
