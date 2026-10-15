"""utils - the temp bucket, for callers that must not carry the bytes themselves.

One job: move a file between two processes that are neither of them a workflow.
A producer (today, the composer session) serves what it rendered; this server
fetches it and puts it in the temp bucket, and answers with the key and the size.
The workflow that asked holds coordinates and never content, which is the whole
point - n8n persists an execution's binaries as well as its JSON, so a workflow
that fetches and uploads an asset keeps a copy of it on the way past.

Two tools, and no others. `temp_store` writes one object, `temp_retrieve` signs a
URL that downloads one. Nothing here lists, copies or deletes: the deployment
prunes the bucket on a schedule, and a delete tool would be a second way to lose
an asset that a reviewer has already approved.

See ../../README.md, and `mcp_runner.s3` for why the key policy lives in code.
"""

from __future__ import annotations

from mcp_runner.server import Registry

from .tools import temp


def register(registry: Registry) -> None:
    registry.add(
        "temp_store",
        temp.STORE_DESCRIPTION,
        temp.temp_store,
        schema=temp.STORE_SCHEMA,
        budget=temp.STORE_BUDGET,
    )
    registry.add(
        "temp_retrieve",
        temp.RETRIEVE_DESCRIPTION,
        temp.temp_retrieve,
        schema=temp.RETRIEVE_SCHEMA,
        budget=temp.RETRIEVE_BUDGET,
    )
