from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings, ConfigManager as Config, Pools


class Attachments:
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
        self.config = config
        self.mappings = mappings
        self.pools = pools
    
    def import_all_attachments(self) -> Mappings:
        """No separate bulk extract step: Zephyr files are downloaded during case import.

        ``Mappings.attachments_map`` is populated per file in
        ``Cases._zephyr_attachments_import`` after each Qase upload
        (flex ``fileId`` → ``filename``, ``hash``, ``url``).
        """
        return self.mappings