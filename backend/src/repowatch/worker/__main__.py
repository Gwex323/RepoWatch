import argparse
import json
import time
from pathlib import Path

from repowatch.api.host import HostServices
from repowatch.application.credentials import CredentialError
from repowatch.application.storage import StorageError
from repowatch.configuration import StorageSettings
from repowatch.worker.service import Worker


def main() -> int:
    parser = argparse.ArgumentParser(description="RepoWatch 串行 Worker；不会隐式初始化数据库")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    services = None
    try:
        services = HostServices(
            StorageSettings(args.data_dir) if args.data_dir else StorageSettings.from_environment()
        )
        services.reconcile_startup()
        worker = Worker(services)
        while True:
            result = worker.run_once()
            if result["state"] != "idle" or args.once:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                break
            if result["state"] == "reconciliation_required":
                return 2
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    except (StorageError, CredentialError):
        print(json.dumps({"state": "host_storage_or_credential_unavailable"}), flush=True)
        return 2
    finally:
        if services is not None:
            services.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
