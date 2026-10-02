"""Turn sorting decisions into hierarchical routing labels.

A destination such as `school/economics/EC2101` is three decisions from one
document: root -> school, school -> economics, economics -> EC2101. When the
chosen folder has children of its own, a stop decision records that the
document belongs in the parent rather than any child. These decisions share
one document, so dataset splits must happen per document, not per decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from services.file_sorter.library import DISCARD_FOLDER, STOP, folder_parts
from services.file_sorter.repository import Decision, Project


def decisions_for(label: str, folders: Iterable[str]) -> list[dict[str, str]]:
    if label == DISCARD_FOLDER:
        return [{"parent": "", "choice": DISCARD_FOLDER}]
    steps = []
    prefix = ""
    for part in folder_parts(label):
        steps.append({"parent": prefix, "choice": part})
        prefix = f"{prefix}/{part}" if prefix else part
    has_children = any(
        folder.startswith(f"{prefix}/") if prefix else True for folder in folders
    )
    if has_children or not prefix:
        steps.append({"parent": prefix, "choice": STOP})
    return steps


def export_rows(
    decisions: list[Decision],
    folders: list[str],
    existing: Iterable[tuple[str, str, str]],
    projects: Mapping[int, Project],
    target: str,
    inventory: Mapping[str, dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield owner decisions, then files that were already in the sorted tree.

    `source_path` records where an entry sat in its dump. It is context for a
    future model, never the label: the label is the tree position alone.
    """
    existing = list(existing)
    present = {f"{folder}/{name}" if folder else name for folder, name, _ in existing}
    placed = set()
    for decision in decisions:
        indexed = inventory.get(decision.destination) if inventory is not None else None
        same_document = indexed is None or (
            indexed["present"] and indexed["document_id"] == decision.document_id
        )
        if same_document:
            placed.add(decision.destination)
        verified = bool(
            indexed
            and same_document
            and indexed["verified"]
            and indexed["current"]
            # An upgrade baseline hashes today's bytes, not the labelled ones.
            and indexed.get("reason") != "legacy_baseline"
        )
        reviewed = bool(same_document and (indexed["reviewed"] if indexed else True))
        status = "present" if decision.destination in present else "missing"
        if not same_document:
            status = "replaced"
        origin = (
            decision.origin_project_id
            if decision.previous_destination is not None
            else decision.project_id
        )
        project = projects.get(origin) if origin is not None else None
        yield {
            "document_id": decision.document_id or decision.id,
            "decision_id": decision.id,
            "replaces_decision_id": decision.replaces_id,
            "duplicate_group_id": decision.duplicate_group_id,
            "duplicate_of_document_id": decision.duplicate_of_document_id,
            "duplicate_eligible": decision.duplicate_of_document_id is None
            and not (indexed and indexed.get("duplicate")),
            "training_eligible": bool(
                verified
                and reviewed
                and status == "present"
                and decision.duplicate_of_document_id is None
                and not (indexed and indexed.get("duplicate"))
            ),
            "classification_provenance": indexed["provenance"]
            if indexed and same_document
            else "manual",
            "needs_review": not reviewed,
            "content_verified": verified,
            "review_type": decision.review_type,
            "file_mtime_ns": decision.file_mtime_ns,
            "tree": decision.target,
            "relative_path": decision.destination,
            "file_path": f"{decision.target}/{decision.destination}"
            if same_document
            else None,
            "expected_file_path": f"{decision.target}/{decision.destination}",
            "file_status": status,
            "name": decision.final_name,
            "original_name": decision.original_name.rpartition("/")[2],
            "kind": decision.kind,
            "sha256": indexed["sha256"] if verified and indexed else decision.sha256,
            "size": indexed["size"] if verified and indexed else decision.size,
            "label": decision.label,
            "decided_label": decision.decided_label,
            "decisions": decisions_for(decision.label, folders),
            "label_source": "owner_reclassified"
            if decision.previous_destination is not None
            else "owner_sorted",
            "decided_at": decision.decided_at,
            "project": project.name if project else None,
            "source": project.source if project else None,
            "source_path": decision.original_name if project else None,
        }
    for folder, name, kind in existing:
        relative = f"{folder}/{name}" if folder else name
        if relative in placed:
            continue
        indexed = inventory.get(relative) if inventory is not None else None
        yield {
            # Pending first scans may not have assigned a discovery ID yet.
            "document_id": indexed["document_id"]
            if indexed and indexed["present"]
            else None,
            "decision_id": None,
            "replaces_decision_id": None,
            "tree": target,
            "relative_path": relative,
            "file_path": f"{target}/{relative}",
            "file_status": "present",
            "name": name,
            "original_name": name,
            "kind": kind,
            "sha256": indexed["sha256"]
            if indexed and indexed["verified"] and indexed["current"]
            else None,
            "size": indexed["size"] if indexed else None,
            "duplicate_eligible": True,
            "training_eligible": False,
            "classification_provenance": "discovered",
            "needs_review": True,
            "content_verified": bool(
                indexed and indexed["verified"] and indexed["current"]
            ),
            "label": folder,
            "decided_label": folder,
            "decisions": decisions_for(folder, folders),
            "label_source": "pre_existing",
            "decided_at": None,
            "project": None,
            "source": None,
            "source_path": None,
        }
