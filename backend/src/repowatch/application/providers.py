"""整批新增／显式编辑与宿主凭据的跨存储准备、激活和清理。"""

import json
from hashlib import sha256
from uuid import uuid4

from repowatch.adapters.windows_jobs import current_process, process_identity
from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.credentials import CredentialError, CredentialVault, SecretValue
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import business_event, required
from repowatch.domain.providers import ProviderInputError, parse_providers
from repowatch.domain.task_records import canonical_json
from repowatch.security import DEFAULT_REDACTOR


class ProviderSaveError(RuntimeError):
    def __init__(self):
        super().__init__("供应商整批保存未激活；请查看脱敏对账状态")


class ProviderService:
    def __init__(self, store, vault: CredentialVault):
        self.store, self.vault = store, vault

    def list(self):
        with self.store.read() as records:
            return [self._public(records, row) for row in records.find("providers", state="active")]

    @staticmethod
    def _public(records, provider):
        revision = required(records, "provider_revisions", id=provider["active_revision_id"])
        return {
            "id": provider["id"],
            "revision": provider["revision"],
            "revision_id": revision["id"],
            "configuration": DEFAULT_REDACTOR.clean(json.loads(revision["configuration_json"])),
            "credential_revision": revision["credential_revision"],
            "credential_configured": True,
        }

    def revision(self, revision_id):
        """受信任适配层的不可变配置引用；不读取或返回凭据本身。"""
        with self.store.read() as records:
            row = dict(required(records, "provider_revisions", id=revision_id))
            if row["state"] != "active":
                raise PermissionError("供应商修订未激活")
            return row

    def status(self, batch_id):
        with self.store.read() as records:
            batch = required(records, "provider_batches", id=batch_id)
            return {
                "batch_id": batch_id,
                "state": batch["state"],
                "providers": json.loads(batch["result_json"] or "[]"),
            }

    def save(
        self,
        operation_id,
        payload,
        *,
        provider_id=None,
        expected_revision=None,
        actor="local-user",
    ):
        human(actor)
        entries = parse_providers(payload)
        if provider_id is not None and (len(entries) != 1 or type(expected_revision) is not int):
            raise ProviderInputError()
        if provider_id is None and any(item.api_key is None for item in entries):
            raise ProviderInputError()
        secrets = {}
        for index, entry in enumerate(entries):
            if entry.api_key is not None:
                secrets[index] = SecretValue(entry.api_key.get_secret_value())
                DEFAULT_REDACTOR.register(secrets[index])
        if any(
            DEFAULT_REDACTOR.clean(entry.public_configuration()) != entry.public_configuration()
            for entry in entries
        ):
            raise ProviderInputError()
        request = {
            "kind": "provider_save",
            "provider_id": provider_id,
            "expected_revision": expected_revision,
            "actor": actor,
            "entries": [
                {
                    "configuration": item.public_configuration(),
                    "credential_digest": sha256(secrets[index].reveal().encode()).hexdigest()
                    if index in secrets
                    else None,
                }
                for index, item in enumerate(entries)
            ],
        }
        batch_id = str(uuid4())
        credential_writes = []
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                batch_id = replay["batch_id"]
            else:
                old = None
                if provider_id is not None:
                    old = required(records, "providers", id=provider_id)
                    if old["state"] != "active" or old["revision"] != expected_revision:
                        raise ConflictError("供应商配置已变化，请刷新")
                for entry in entries:
                    conflicts = [
                        row
                        for row in records.find("providers", name_key=entry.name.casefold())
                        if row["state"] != "aborted" and row["id"] != provider_id
                    ]
                    if conflicts or records.one(
                        "provider_name_claims", name_key=entry.name.casefold()
                    ):
                        raise ConflictError("供应商名称已存在或正在保存，整批未保存")
                remember_operation(
                    records,
                    operation_id,
                    fingerprint,
                    expected_revision or 1,
                    {
                        "batch_id": batch_id,
                    },
                )
                records.add(
                    "provider_batches",
                    {
                        "id": batch_id,
                        "operation_id": operation_id,
                        "state": "preparing",
                        "owner_process_json": canonical_json(current_process()),
                    },
                )
                for index, entry in enumerate(entries):
                    identifier = provider_id or str(uuid4())
                    previous = (
                        required(records, "provider_revisions", id=old["active_revision_id"])
                        if old
                        else None
                    )
                    revision = (
                        max(
                            (
                                row["revision"]
                                for row in records.find(
                                    "provider_revisions",
                                    provider_id=identifier,
                                )
                            ),
                            default=0,
                        )
                        + 1
                    )
                    if index in secrets:
                        reference = "credential-" + uuid4().hex
                        credential_revision = revision
                        records.add(
                            "credential_stages",
                            {
                                "reference": reference,
                                "batch_id": batch_id,
                                "state": "planned",
                            },
                        )
                        credential_writes.append((reference, secrets[index]))
                    else:
                        reference = previous["credential_reference"]
                        credential_revision = previous["credential_revision"]
                    if not old:
                        records.add(
                            "providers",
                            {
                                "id": identifier,
                                "name": entry.name,
                                "name_key": entry.name.casefold(),
                                "revision": revision,
                                "state": "staged",
                            },
                        )
                    records.add(
                        "provider_name_claims",
                        {
                            "name_key": entry.name.casefold(),
                            "batch_id": batch_id,
                        },
                    )
                    records.add(
                        "provider_revisions",
                        {
                            "id": str(uuid4()),
                            "provider_id": identifier,
                            "revision": revision,
                            "base_revision": old["revision"] if old else None,
                            "batch_id": batch_id,
                            "configuration_json": canonical_json(entry.public_configuration()),
                            "credential_reference": reference,
                            "credential_revision": credential_revision,
                            "state": "staged",
                        },
                    )
        if replay is not None:
            return self.status(batch_id)
        try:
            for reference, secret in credential_writes:
                self.vault.create(reference, secret)
                if self.vault.read(reference).reveal() != secret.reveal():
                    raise CredentialError()
                with self.store.write() as records:
                    records.change(
                        "credential_stages", {"reference": reference}, {"state": "stored"}
                    )
            self._activate(batch_id)
        except Exception:
            try:
                self._abort(batch_id)
            except Exception:
                # Preparing batch and planned references remain for startup reconciliation.
                pass
            raise ProviderSaveError() from None
        return self.status(batch_id)

    def _activate(self, batch_id):
        with self.store.write() as records:
            batch = required(records, "provider_batches", id=batch_id)
            if batch["state"] != "preparing":
                raise ConflictError("批次不处于可激活状态")
            if any(
                row["state"] != "stored"
                for row in records.find("credential_stages", batch_id=batch_id)
            ):
                raise CredentialError()
            result = []
            for revision in records.find("provider_revisions", batch_id=batch_id):
                provider = required(records, "providers", id=revision["provider_id"])
                if (
                    revision["base_revision"] is not None
                    and provider["revision"] != revision["base_revision"]
                ):
                    raise ConflictError("准备期间供应商已被修改")
                config = json.loads(revision["configuration_json"])
                records.change("provider_revisions", {"id": revision["id"]}, {"state": "active"})
                records.change(
                    "providers",
                    {"id": provider["id"]},
                    {
                        "active_revision_id": revision["id"],
                        "revision": revision["revision"],
                        "name": config["name"],
                        "name_key": config["name"].casefold(),
                        "state": "active",
                    },
                )
                result.append(
                    self._public(records, required(records, "providers", id=provider["id"]))
                )
            for credential in records.find("credential_stages", batch_id=batch_id):
                records.change(
                    "credential_stages", {"reference": credential["reference"]}, {"state": "active"}
                )
            records.change(
                "provider_batches",
                {"id": batch_id},
                {
                    "state": "active",
                    "result_json": canonical_json(result),
                },
            )
            records.remove("provider_name_claims", batch_id=batch_id)
            business_event(records, "providers_activated", batch_id=batch_id, count=len(result))

    def _abort(self, batch_id):
        with self.store.write() as records:
            batch = required(records, "provider_batches", id=batch_id)
            if batch["state"] == "active":
                return
            for revision in records.find("provider_revisions", batch_id=batch_id):
                if revision["state"] == "staged":
                    records.change(
                        "provider_revisions", {"id": revision["id"]}, {"state": "aborted"}
                    )
                provider = required(records, "providers", id=revision["provider_id"])
                if provider["state"] == "staged":
                    records.change("providers", {"id": provider["id"]}, {"state": "aborted"})
            records.change("provider_batches", {"id": batch_id}, {"state": "aborted"})
            records.remove("provider_name_claims", batch_id=batch_id)
            references = [
                row["reference"]
                for row in records.find("credential_stages", batch_id=batch_id)
                if row["state"] not in {"active", "deleted"}
            ]
        for reference in references:
            self.vault.delete(reference)
            with self.store.write() as records:
                records.change("credential_stages", {"reference": reference}, {"state": "deleted"})

    def reconcile(self):
        """启动时仅清理已确认退出的准备者；保留健康保存进程及所有旧活动凭据。"""
        with self.store.read() as records:
            batches = [
                dict(row) for row in records.find("provider_batches") if row["state"] != "active"
            ]
        result = []
        for batch in batches:
            owner = json.loads(batch["owner_process_json"])
            dead = False
            if owner.get("platform") == "windows" and owner.get("created") is not None:
                try:
                    dead = process_identity(owner["pid"]) != owner["created"]
                except OSError:
                    pass
            if batch["state"] == "aborted" or dead:
                try:
                    self._abort(batch["id"])
                    result.append({"batch_id": batch["id"], "state": "cleaned"})
                except (CredentialError, StorageError):
                    result.append({"batch_id": batch["id"], "state": "cleanup_required"})
        return result
