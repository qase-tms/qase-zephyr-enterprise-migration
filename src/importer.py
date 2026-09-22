from concurrent.futures import ThreadPoolExecutor

from .entities.zephyr_enterprise import (
    Users,
    Fields,
    Projects,
    Attachments,
    Suites,
    Cases,
    Runs,
    Milestones,
    Configurations,
    SharedSteps,
)
from .service import DryRunQaseService, QaseService, QaseScimService, ZephyrEnterpriseService
from .support import ConfigManager, Logger, Mappings, ThrottledThreadPoolExecutor, Pools

# Zephyr work is network I/O–bound (attachment/list prefetch, downloads). Use a fixed pool size
# like the old default (16); capping by CPU count slowed migrations on multi-core laptops.
_ZEPHYR_SOURCE_POOL_WORKERS = 16


class Importer:
    def __init__(self, config: ConfigManager, logger: Logger, dry_run: bool = False) -> None:
        z_workers = _ZEPHYR_SOURCE_POOL_WORKERS
        self.pools = Pools(
            qase_pool=ThrottledThreadPoolExecutor(max_workers=8, requests=250, interval=12),
            source_pool=ThreadPoolExecutor(max_workers=z_workers),
        )

        self.logger = logger
        self.config = config
        self.dry_run = dry_run
        self.qase_scim_service = None

        if dry_run:
            self.logger.log(
                "[Migration] DRY RUN: reading everything from Zephyr Enterprise, "
                "writing nothing to Qase."
            )
            print("\n\033[33m▲ DRY RUN: nothing will be written to Qase\033[0m\n", flush=True)
            self.qase_service = DryRunQaseService(config, logger)
        else:
            self.qase_service = QaseService(config, logger)
            if config.get("qase.scim_token"):
                self.qase_scim_service = QaseScimService(config, logger)

        self.source_service = ZephyrEnterpriseService(config, logger)
        self.active_project_code = None

        # users.default accepts an email or a numeric id; resolve
        # it once here so a bad value fails before any project is touched.
        self.default_user_id = self.qase_service.resolve_user_id(self.config.get("users.default"))
        self.mappings = Mappings("zephyr-enterprise", self.default_user_id)

        # Every warn and error becomes a line in the end-of-run migration report,
        # so a new warning is a new report line by construction.
        self.logger.attach_stats(self.mappings.stats)

    def start(self):
        # Step 1. Build users map
        if self.config.get("users.migrate", True):
            self.mappings = Users(
                self.qase_service,
                self.source_service,
                self.logger,
                self.mappings,
                self.config,
                self.pools,
                self.qase_scim_service,
            ).import_users()
        else:
            self.logger.log("[Users] Skipping user migration (users.migrate is false).")

        # Step 2. Import project and build projects map
        self.mappings = Projects(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_projects()

        # Step 3. Attachments. No bulk extract: Zephyr files are downloaded
        # and uploaded during case import; this step only seeds the registry.
        self.mappings = Attachments(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_all_attachments()

        # Step 4. Import custom fields
        self.mappings = Fields(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_fields()

        # Step 5. Import projects data in parallel
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(self.import_project_data, project)
                for project in self.mappings.projects
            ]
            for future in futures:
                future.result()

        prefix = str(self.config.get("prefix") or "zephyr-enterprise")
        self.mappings.stats.print()
        self.mappings.stats.print_issues()
        self.mappings.stats.save(prefix)
        self.mappings.stats.save_xlsx(prefix)
        print(f"\nqase-zephyr-enterprise-migration v{self.logger.version}")
        print(f"Statistics written to stats/{prefix}_stats.json and stats/{prefix}_stats.xlsx")
        if self.logger.log_file:
            print(f"Full log: {self.logger.log_file}")

    def import_project_data(self, project):
        self.logger.print_group(
            f'Importing project: {project["name"]}'
            + (
                " (" + project["suite_title"] + ")"
                if "suite_title" in project
                else ""
            )
        )

        self.mappings = Configurations(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.pools,
        ).import_configurations(project)

        self.mappings = SharedSteps(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.pools,
        ).import_shared_steps(project)

        self.mappings = Milestones(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
        ).import_milestones(project)

        self.mappings = Suites(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_suites(project)

        Cases(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_cases(project)

        self.mappings = Runs(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            project,
            self.pools,
        ).import_runs()
