from .stats import Stats


class Mappings:
    def __init__(self, source: str, default_user: int = 1):
        self.suites = {}
        self.users = {}
        self.types = {}
        self.priorities = {}
        self.result_statuses = {}
        self.case_statuses = {}
        self.custom_fields = {}
        # Zephyr field definition id (GET ``field/entity/TestCase`` ``id``) → field dict
        self.custom_fields_by_zephyr_id: dict = {}
        self.milestones = {}
        self.configurations = {}
        self.projects = []
        self.attachments_map = {}
        self.shared_steps = {}

        # Zephyr testcase.id (from tree API) -> Qase case id after bulk import (per project code)
        self.zephyr_tc_id_to_qase_case_id: dict = {}

        # Zephyr Enterprise project ids -> Qase project codes
        self.project_map = {}
        # Step fields: used to tell step-shaped custom fields from plain case fields
        self.step_fields = []

        self.refs_id = None
        self.group_id = None

        self.zephyr_enterprise_fields_type = {
            1: 2,
            2: 2,
            3: 3,
            4: 4,
            5: 9,
            6: 0,
            7: 0,
            8: 1,
            10: 0,
        }

        self.qase_fields_type = {
            "number": 0,
            "string": 1,
            "text": 2,
            "selectbox": 3,
            "checkbox": 4,
            "radio": 5,
            "multiselect": 6,
            "url": 7,
            "user": 8,
            "datetime": 9,
        }

        self.default_user = default_user
        self.stats = Stats(source=source)

        # Filled from Qase GET system fields (see Fields.import_fields_async)
        self.qase_priority_keys_to_id: dict = {}
        self.qase_case_status_keys_to_id: dict = {}

    def register_qase_system_fields(self, fields: list) -> None:
        """Map Qase priority / case-status option titles and slugs → numeric ids for bulk import."""
        self.qase_priority_keys_to_id.clear()
        self.qase_case_status_keys_to_id.clear()
        for f in fields or []:
            if not isinstance(f, dict):
                continue
            slug = (f.get("slug") or "").strip().lower()
            title = (f.get("title") or "").strip().lower()
            options = f.get("options")
            if not isinstance(options, list) or not options:
                continue
            target = None
            # Qase slugs vary (e.g. "priority", "case-priority"); avoid "severity".
            if (
                slug == "priority"
                or title == "priority"
                or ("priority" in slug and "severity" not in slug)
            ):
                target = self.qase_priority_keys_to_id
            elif slug in ("status", "case-status", "state") or (
                "status" in slug and ("case" in slug or slug.endswith("status"))
            ):
                target = self.qase_case_status_keys_to_id
            elif title in ("status", "state") or (
                "status" in title and "run" not in title and "defect" not in title
            ):
                target = self.qase_case_status_keys_to_id
            if target is None:
                continue
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                oid = opt.get("id")
                if oid is None:
                    continue
                oid = int(oid)
                for key in (opt.get("slug"), opt.get("title")):
                    if isinstance(key, str) and key.strip():
                        target[key.strip().lower()] = oid
                # Zephyr often sends priority as "1","2","3" matching Qase option ids.
                target[str(oid)] = oid

    def register_zephyr_custom_field(self, field: dict) -> None:
        """Index a Zephyr custom field for lookup by API name, display name, list key, and id."""
        if not isinstance(field, dict):
            return
        for key in (field.get("fieldName"), field.get("displayName"), field.get("name")):
            if key:
                self.custom_fields[key] = field
        zid = field.get("id")
        if zid is not None:
            try:
                self.custom_fields_by_zephyr_id[int(zid)] = field
            except (TypeError, ValueError):
                pass

    def get_user_id(self, id: int) -> int:
        if (id in self.users):
            return self.users[id]
        return self.default_user

    def register_zephyr_testcase_qase_case_id(
        self, project_code: str, zephyr_testcase_id: int, qase_case_id: int
    ) -> None:
        """Record mapping from Zephyr testcase id to Qase case id (for runs / results import)."""
        if not project_code or zephyr_testcase_id is None or qase_case_id is None:
            return
        code = str(project_code).strip()
        self.zephyr_tc_id_to_qase_case_id.setdefault(code, {})[
            int(zephyr_testcase_id)
        ] = int(qase_case_id)