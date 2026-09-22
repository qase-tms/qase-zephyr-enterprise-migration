import asyncio
import html

from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings, ConfigManager as Config, Pools

from .attachments import Attachments

from typing import List, Optional


class Suites:
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
        self.attachments = Attachments(self.qase, self.zephyr, self.logger, self.mappings, self.config, self.pools)

        self.suites_map = {}
        self.logger.divider()

        self.roots = []
        self.children = []
        self.fake_index = 1000000

    def import_suites(self, project) -> Mappings:
        return asyncio.run(self.import_suites_async(project))
    
    async def import_suites_async(self, project):
        self.logger.log(f'[{project["code"]}][Suites] Importing suites from Zephyr Enterprise project {project["name"]}')
        async with asyncio.TaskGroup() as tg:
            # Importing project folders
            root_suites = await self.pools.source(self.zephyr.get_root_suites, project['zephyr_id'])
            for suite in root_suites:
                tg.create_task(
                    self.import_suite(suite.get("description") or "", project, suite)
                )

            # Importing folders from the releases
            releases = await self.pools.source(self.zephyr.get_releases, project['zephyr_id'])
            for release in releases:
                tg.create_task(self.import_release(release, project))

        self.mappings.suites[project['code']] = self.suites_map
        
        return self.mappings
    
    async def import_release(self, release, project):
        self.logger.log(f'[{project["code"]}][Suites] Importing release {release["name"]}')
        # Create release suite
        await self._create_suite(project['code'], release['name'], description=release.get('description', ''), zephyr_suite_id=self.fake_index+release['id'])

        release_suites = await self.pools.source(self.zephyr.get_suites_by_release, release['id'])
        for suite in release_suites:
            await self.import_suite('', project, suite, parent_id=self.fake_index+release['id'])

    async def import_suite(self, description, project, suite, parent_id=None):
        await self._create_suite(
            project["code"],
            suite["name"],
            description=description or suite.get("description") or "",
            zephyr_suite_id=suite["id"],
            parent_id=parent_id,
        )
        # Get children
        await self._create_children(project['code'], suite['id'])

    async def _create_children(self, project_code, suite_id):
        # Get hierarchy tree for the suite (direct children only — recurse so nested
        # folders like Authentication/Login/Valid get Qase suites and case import runs).
        children = await self.pools.source(self.zephyr.get_children, suite_id)
        if suite_id in children:
            children.remove(suite_id)
        if len(children) > 0:
            for child_id in children:
                child = await self.pools.source(self.zephyr.get_suite, child_id)
                await self._create_suite(
                    project_code,
                    child["name"],
                    description=child.get("description") or "",
                    zephyr_suite_id=child["id"],
                    parent_id=suite_id,
                )
                await self._create_children(project_code, child_id)

    async def _create_suite(
            self, 
            qase_code: str, 
            title: str, 
            description: Optional[str], 
            parent_id: Optional[int] = None, 
            zephyr_suite_id: Optional[int] = None
    ):
        description = html.unescape(description) if description else ""
        #description = self.attachments.check_and_replace_attachments(description, qase_code)
        parent_id = self.suites_map.get(parent_id, None) if parent_id else None

        self.suites_map[zephyr_suite_id] = await self.pools.qs(
            self.qase.create_suite,
            qase_code.upper(),
            title,
            description,
            parent_id,
        )
        self.mappings.stats.add_entity_count(qase_code, 'suites', 'qase')