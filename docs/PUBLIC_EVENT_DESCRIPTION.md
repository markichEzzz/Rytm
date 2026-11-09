# Moderator-owned public event copy

EventCandidate.description remains an internal extraction/submission draft.
EventCandidate.public_description is a separate, blank-by-default moderator override.
The Admin's Final public description textarea initially shows generated factual copy
(or the existing description for a direct user submission). Saving unchanged generated
copy leaves the override empty, allowing subsequent fact changes to regenerate it.
Editing the textarea stores the final copy; clearing it restores automatic fallback.

Approval still validates permissions, fields, photo rights, location and ambiguity
confirmation. Editing copy never approves a candidate. After validation, approval
copies the nonblank public override to Event.description, otherwise using the existing
factual generator for ingested candidates or original direct-submission description.
The existing Event serializer exposes Event.description; Android already displays
that API value as plain text under Про подію. No extraction notes or private metadata
are added to public copy. Approved candidates remain read-only; later event edits
use the linked Event admin as before.

Automatic extraction/repair never writes public_description. The new field is isolated
from automatic facts/draft updates and does not change extraction or collection rules.
Existing candidates keep blank overrides and retain their current publication behavior.

Migration: ingestion/0009_eventcandidate_public_description.py. It adds one blank-by-
default TextField. Django tests apply it only to the isolated local test database.
No production migration, deployment, commit or push is performed by this task.
