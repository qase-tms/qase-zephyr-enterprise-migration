import asyncio

from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings, ConfigManager as Config, Pools

from typing import Optional

import re

class Projects:
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
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.pools = pools
        self.existing_codes = set()
        self.existing_projects_by_title = {}  # title_lower -> existing Qase code (for reuse)
        self.logger.divider()

    def import_projects(self) -> Mappings:
        return asyncio.run(self.import_projects_async())

    async def import_projects_async(self) -> Mappings:
        self.logger.log('Importing projects from Zephyr Enterprise')

        await self._load_existing_qase_projects()

        zephyr_projects = await self._get_all_projects()
        if zephyr_projects:
            total = len(zephyr_projects)
            self.logger.log(f'Found {str(total)} projects')
            self.logger.print_status('Importing projects', total=total)

            async with asyncio.TaskGroup() as tg:
                for i, project in enumerate(zephyr_projects):
                    tg.create_task(self.import_project(i, project, total))
        else:
            self.logger.log('No projects found in Zephyr Enterprise')
        return self.mappings
    
    async def import_project(self, i, project, total):
        self.logger.log(f'Importing project: {project["name"]}')
        if self._check_import(project['name']):
            data = {
                "zephyr_id": project['id'],
                "name": project['name']
            }
            code = await self._create_project(project['name'], '')
            if code:
                data['code'] = code
                self.mappings.projects.append(data)
                self.mappings.project_map[project['id']] = data['code']
                self.mappings.stats.add_project(code, project['name'])
            else:
                self.logger.log(f'Failed to create project: {project["name"]}', 'error')
        else:
            self.logger.log(f'Skipping project: {project["name"]}')
        self.logger.print_status('Importing projects', i, total)
    
    async def _get_all_projects(self):
        return self.zephyr.get_projects()

    async def _load_existing_qase_projects(self):
        """Index existing Qase projects by title so they can be reused instead of recreated.

        Project creation may be restricted for the API token (admin-only). Reusing a
        project that already exists (created manually or on a prior run) lets the rest
        of the migration proceed. Existing codes are also recorded so any generated
        code for a brand-new project avoids colliding with one already in the workspace.
        """
        limit, offset = 100, 0
        while True:
            result = await self.pools.qs(self.qase.get_projects, limit, offset)
            entities = getattr(result, "entities", None) if result else None
            if not entities:
                break
            for p in entities:
                code = getattr(p, "code", None)
                title = getattr(p, "title", None)
                if code:
                    self.existing_codes.add(code.upper())
                    if title:
                        self.existing_projects_by_title[title.strip().lower()] = code
            if len(entities) < limit:
                break
            offset += limit
        self.logger.log(
            f"Found {len(self.existing_projects_by_title)} existing Qase project(s) available for reuse"
        )

    def _match_existing_project(self, title: str) -> Optional[str]:
        """Return the code of an existing Qase project with the same title (case-insensitive)."""
        return self.existing_projects_by_title.get((title or "").strip().lower())
    
    # Function checks if the project should be imported
    def _check_import(self, title: str) -> bool:
        """Project selection: projects.import_all + projects.import + projects.exclude.

        Matching is on the Zephyr project name, case-insensitively, because that
        is what the customer reads off the Zephyr UI. ``projects.exclude`` always
        wins, so it works whether the candidate list came from import_all or from
        import.
        """
        name = (title or "").strip().lower()

        exclude = {
            str(p).strip().lower()
            for p in (self.config.get("projects.exclude") or [])
            if str(p).strip()
        }
        if name in exclude:
            return False

        if self.config.get("projects.import_all"):
            return True

        include = {
            str(p).strip().lower()
            for p in (self.config.get("projects.import") or [])
            if str(p).strip()
        }
        if not include:
            return True
        return name in include
    
    # Method generates short code that will be used as a project code in from a string    
    def _short_code(self, s: str) -> str:
        s = s.replace("-", " ")  # Replace dashes with spaces

        # Remove all characters except letters
        s = re.sub('[^a-zA-Z ]', '', s)

        words = s.split()
        # Ensure the first character is a letter and make it uppercase
        if len(words) > 1:  # if the string contains multiple words
            code = ''.join(word[0] for word in words).upper()
        else:
            code = s.upper()

        code = code.replace(" ", "")

        # Truncate to 10 characters
        code = code[:10]

        # Handle duplicates by adding a letter postfix
        original_code = code
        postfix = ''
        while code in self.existing_codes or len(code) < 2:
            postfix = self._next_postfix(postfix)
            code = (original_code[:10-len(postfix)] + postfix).upper()

        self.existing_codes.add(code)
        return code

    def _next_postfix(self, postfix):
        if not postfix:
            return 'A'  # Start with 'A' if no postfix
        elif postfix[-1] == 'Z':  # If last char is 'Z', increment previous char
            return self._next_postfix(postfix[:-1]) + 'A'
        else:  # Increment the last character
            return postfix[:-1] + chr(ord(postfix[-1]) + 1)
    
    # Method creates project in Qase
    async def _create_project(self, title: str, description: Optional[str]) -> Optional[str]:
        existing = self._match_existing_project(title)
        if existing:
            self.logger.log(
                f"Project already exists in Qase: {title} [{existing}]; reusing existing project."
            )
            self.existing_codes.add(existing.upper())
            return existing

        code = self._mapped_code(title) or self._short_code(title)
        if await self.pools.qs(self.qase.create_project, title, description, code, self.mappings.group_id):
            return code
        return None

    def _mapped_code(self, title: str) -> Optional[str]:
        """``projects.mapping``: Zephyr project name to a specific Qase project code.

        Used when the target project code has to be chosen rather than derived
        from the title. An existing Qase project with the same title still wins,
        so this only affects projects the migration creates.
        """
        raw = self.config.get("projects.mapping")
        if not isinstance(raw, dict) or not raw:
            return None
        lookup = {str(k).strip().lower(): v for k, v in raw.items()}
        value = lookup.get((title or "").strip().lower())
        if value is None:
            return None
        code = str(value).strip().upper()
        if not code:
            return None
        self.logger.log(f"Project {title!r} will use Qase code {code} from projects.mapping")
        self.existing_codes.add(code)
        return code
