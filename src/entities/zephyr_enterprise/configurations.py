from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings, Pools

import asyncio


class Configurations:
    def __init__(
            self,
            qase_service: QaseService,
            source_service: ZephyrEnterpriseService,
            logger: Logger,
            mappings: Mappings,
            pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.logger = logger
        self.mappings = mappings
        self.pools = pools

        self.map = {}
        self.logger.divider()

    def import_configurations(self, project) -> Mappings:
        return self.mappings