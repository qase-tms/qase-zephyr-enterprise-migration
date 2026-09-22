import asyncio
import json

from qaseio.models import CustomFieldCreateValueInner

from typing import Any, List, Optional

from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings, ConfigManager as Config, Pools


class Fields:
    def __init__(
            self, 
            qase_service: QaseService,
            source_service: ZephyrEnterpriseService,
            logger: Logger,
            mappings: Mappings,
            config: Config,
            pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.logger = logger
        self.mappings = mappings
        self.config = config
        self.pools = pools

        self.refs_id = None
        self.system_fields = []

        self.map = {}
        self.logger.divider()

    @staticmethod
    def _zephyr_field_list_key(field: Any) -> Optional[str]:
        """Stable id for ``cases.fields`` filtering; Zephyr rows may omit ``name``."""
        if not isinstance(field, dict):
            return None
        for k in ("name", "searchFieldName", "fieldName", "displayName"):
            v = field.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        vid = field.get("id")
        if vid is not None:
            return str(vid)
        return None

    @staticmethod
    def _zephyr_field_type_id(field: Any) -> Optional[int]:
        """Zephyr Enterprise exposes ``fieldTypeMetadata``; older code also read ``type_id``."""
        if not isinstance(field, dict):
            return None
        for k in ("fieldTypeMetadata", "type_id", "fieldType", "type"):
            v = field.get(k)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    def import_fields(self):
        return asyncio.run(self.import_fields_async())
    
    async def import_fields_async(self):
        self.logger.log('[Fields] Loading custom fields from Qase')
        qase_custom_fields = await self.pools.qs(self.qase.get_case_custom_fields)
        qase_custom_fields = qase_custom_fields or []

        self.logger.log('[Fields] Loading custom fields from Zephyr Enterprise')
        zephyr_custom_fields = await self.pools.source(self.zephyr.get_case_custom_fields)
        if not isinstance(zephyr_custom_fields, list):
            zephyr_custom_fields = []

        self.logger.log('[Fields] Loading system fields from Qase')
        qase_system_fields = await self.pools.qs(self.qase.get_system_fields)
        qase_system_fields = qase_system_fields or []
        for field in qase_system_fields:
            self.system_fields.append(field.to_dict())
        self.mappings.register_qase_system_fields(self.system_fields)

        #async with asyncio.TaskGroup() as tg:
        #    tg.create_task(self._create_types_map())
        #    tg.create_task(self._create_priorities_map())

        total = len(zephyr_custom_fields)
        
        self.logger.log(f'[Fields] Found {str(total)} custom fields')

        fields_to_import = self._get_fields_to_import(zephyr_custom_fields)

        i = 0
        self.logger.print_status('Importing custom fields', i, total)
        self.mappings.stats.add_custom_field('zephyr-enterprise', total)
        async with asyncio.TaskGroup() as tg:
            for field in zephyr_custom_fields:
                i += 1
                list_key = self._zephyr_field_list_key(field)
                if not list_key:
                    self.logger.log(
                        f"[Fields] Skipping custom field row without name/id keys: {field!r}",
                        "warn",
                    )
                    self.logger.print_status("Importing custom fields", i, total)
                    continue
                visible = field.get("isVisible", True)
                tid = self._zephyr_field_type_id(field)
                field["type_id"] = tid
                reason: Optional[str] = None
                if list_key not in fields_to_import:
                    reason = "not in cases.fields"
                elif not visible:
                    reason = "isVisible=False"
                elif tid not in self.mappings.zephyr_enterprise_fields_type:
                    reason = f"unsupported fieldTypeMetadata={tid!r}"
                if reason is None:
                    tg.create_task(self._create_custom_field(field, qase_custom_fields))
                else:
                    self.logger.log(f"[Fields] Skipping custom field: {list_key} ({reason})")
                self.logger.print_status('Importing custom fields', i, total)

        return self.mappings

    def _get_fields_to_import(self, custom_fields: List) -> List[str]:
        self.logger.log('[Fields] Building a map for fields to import')
        fields_to_import = self.config.get('cases.fields')
        if fields_to_import is None:
            out: List[str] = []
            for f in custom_fields or []:
                k = self._zephyr_field_list_key(f)
                if k:
                    out.append(k)
            return out
        if len(fields_to_import) == 0:
            for field in custom_fields or []:
                k = self._zephyr_field_list_key(field)
                if k:
                    fields_to_import.append(k)
        return fields_to_import

    async def _create_custom_field(self, field, qase_fields):
        display_name = field.get('displayName') or field.get('name') or field.get('fieldName') or ''
        tid = field.get('type_id')
        if tid is None:
            tid = self._zephyr_field_type_id(field)
        zephyr_qase_type = self.mappings.zephyr_enterprise_fields_type.get(tid)

        if qase_fields and len(qase_fields) > 0:
            for qase_field in qase_fields:
                if (
                    qase_field.title == display_name
                    and zephyr_qase_type == self.mappings.qase_fields_type.get(qase_field.type.lower())
                ):
                    self.logger.log('[Fields] Custom field already exists: ' + display_name)
                    if qase_field.type.lower() in ("selectbox", "multiselect", "radio"):
                        field['qase_values'] = {}
                        try:
                            values = json.loads(qase_field.value) if qase_field.value else []
                        except (TypeError, ValueError):
                            values = []
                        for value in values:
                            field['qase_values'][value['id']] = value['title']
                    field['qase_id'] = qase_field.id
                    self.mappings.register_zephyr_custom_field(field)
                    return

        data = self._prepare_custom_field_data(field, self.mappings)
        qase_id = await self.pools.qs(self.qase.create_custom_field, data)
        if qase_id and qase_id > 0:
            self.logger.log('[Fields] Custom field created: ' + display_name)
            field['qase_id'] = qase_id
            self.mappings.register_zephyr_custom_field(field)
            self.mappings.stats.add_custom_field('qase')

    def _prepare_custom_field_data(self, field, mappings) -> dict:
        tid = field.get('type_id')
        if tid is None:
            tid = self._zephyr_field_type_id(field)
        title = field.get('displayName') or field.get('name') or field.get('fieldName') or ''
        data = {
            'title': title,
            'entity': 0,  # 0 - case, 1 - run, 2 - defect,
            'type': self.mappings.zephyr_enterprise_fields_type[tid],
            'value': [],
            'is_filterable': True,
            'is_visible': bool(field.get('isVisible', True)),
            'is_required': bool(field.get('mandatory', False)),
        }

        all_projects = bool(field.get('allProject', True))
        data['is_enabled_for_all_projects'] = all_projects
        if not all_projects:
            zephyr_ids = field.get('projectIds') or []
            codes: list = []
            for zpid in zephyr_ids:
                try:
                    code = self.mappings.project_map.get(int(zpid))
                except (TypeError, ValueError):
                    code = None
                if code and code not in codes:
                    codes.append(code)
            if codes:
                data['projects_codes'] = codes
            else:
                self.logger.log(
                    f'[Fields] Custom field {title!r} is per-project but none of its '
                    f'projectIds={zephyr_ids!r} were migrated to Qase; creating as all-projects',
                    'warn',
                )
                data['is_enabled_for_all_projects'] = True

        default_val = self.__get_default_value(field)
        if default_val:
            data['default_value'] = default_val
        if tid in (6, 12):
            configs = field.get('configs') or []
            if configs and isinstance(configs[0], dict):
                items = (configs[0].get('options') or {}).get('items') or ''
                values = self.__split_values(items) if items else {}
                field['qase_values'] = {}
                for key, value in values.items():
                    data['value'].append(
                        CustomFieldCreateValueInner(
                            id=int(key)+1,  # Zephyr option keys may start at 0
                            title=value,
                        ),
                    )
                    field['qase_values'][int(key)+1] = value
            else:
                self.logger.log(
                    '[Fields] Error creating custom field: ' + title + '. No options found',
                    'warn',
                )
        return data

    def __get_default_value(self, field):
        dv = field.get('defaultValue')
        if dv is None:
            return None
        if isinstance(dv, str):
            return dv
        return str(dv)

    def __split_values(self, string: str, delimiter: str = ',') -> dict:
        """Zephyr picklist options come as a newline-separated ``key,value`` string."""
        result: dict = {}
        if not isinstance(string, str):
            return result
        for line in string.split('\n'):
            if not line:
                continue
            if delimiter not in line:
                continue
            key, value = line.split(delimiter, 1)
            result[key.strip()] = value.strip()
        return result