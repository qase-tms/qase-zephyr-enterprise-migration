from ..repository.zephyr_enterprise import ZephyrEnterpriseApiRepository
from ..api.zephyr_enterprise import ZephyrEnterpriseApiClient


class ZephyrEnterpriseService:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.repository = ZephyrEnterpriseApiRepository(
            ZephyrEnterpriseApiClient(
                base_url=config.get("zephyr.host"),
                token=config.get("zephyr.api_token"),
                logger=logger,
                auth_type=config.get("zephyr.auth") or "bearer",
                basic_username=config.get("zephyr.username"),
                basic_password=config.get("zephyr.password"),
                max_retries=5,
                backoff_factor=5,
            )
        )
        
    def get_projects(self, limit: int = 100, offset: int = 0):
        return self.repository.get_projects(limit, offset)
    
    def get_users(self):
        return self.repository.get_users()

    def search_external_groups(self, name: str = "", pagesize: int = 100):
        """See :meth:`ZephyrEnterpriseApiRepository.search_external_groups`."""
        return self.repository.search_external_groups(name, pagesize)
    
    def get_case_custom_fields(self):
        return self.repository.get_case_custom_fields()
    
    def get_case_system_fields(self):
        return self.repository.get_case_system_fields()
        
    def get_milestones(self, project_id: int, limit: int = 250, offset: int = 0):
        return self.repository.get_milestones(project_id, limit, offset)
    
    def get_root_suites(self, project_id: int, limit: int = 100, offset: int = 0):
        return self.repository.get_root_suites(project_id, limit, offset)
    
    def get_cases(self, suite_id: int = 0, limit: int = 250, offset: int = 0):
        return self.repository.get_cases(suite_id, limit, offset)
    
    def get_cases_for_suite(self, suite_id: int):
        return self.repository.get_cases_for_suite(suite_id)
    
    def get_children(self, tree_id: int):
        return self.repository.get_children(tree_id)
    
    def get_suite(self, tree_id: int):
        return self.repository.get_suite(tree_id)
    
    def get_releases(self, project_id: int):
        return self.repository.get_releases(project_id)

    def search_executions_for_release(
        self,
        release_id: int,
        project_id: int = None,
        cycle_phase_id: int = None,
        offset: int = 0,
        pagesize: int = 100,
    ):
        list_scope = self.config.get("zephyr.execution_list_scope")
        return self.repository.search_executions_for_release(
            release_id,
            project_id=project_id,
            cycle_phase_id=cycle_phase_id,
            offset=offset,
            pagesize=pagesize,
            list_scope=list_scope,
        )

    def get_cycles_for_release(self, project_id: int, release_id: int):
        return self.repository.get_cycles_for_release(project_id, release_id)

    def get_cycle(self, cycle_id: int):
        return self.repository.get_cycle(cycle_id)

    def get_executions_by_cycle_phase(self, cycle_id: int, phase_id: int):
        return self.repository.get_executions_by_cycle_phase(cycle_id, phase_id)

    def search_executions_advancesearch(
        self, release_id: int, firstresult: int, maxresults: int, word: str = None
    ):
        w = word if word is not None else self.config.get("runs.advancesearch_word", "*")
        if w is None or (isinstance(w, str) and not w.strip()):
            w = "*"
        append_zql = bool(self.config.get("runs.advancesearch_append_zql_word", False))
        log_diag = bool(self.config.get("runs.log_zephyr_executions", False))
        return self.repository.search_executions_advancesearch(
            release_id,
            firstresult,
            maxresults,
            word=str(w),
            append_zql_word=append_zql,
            log_diagnostics=log_diag,
        )

    def get_suites_by_release(self, release_id: int):
        return self.repository.get_suites_by_release(release_id)

    def get_attachments_case(self, case_id: int):
        return self.repository.get_attachments_case(case_id)

    def get_attachments_case_merged(
        self,
        testcase_id: int = None,
        catalog_node_id: int = None,
        *,
        extended_probe: bool = False,
        skip_list_fetch: bool = False,
    ):
        return self.repository.get_attachments_case_merged(
            testcase_id,
            catalog_node_id,
            extended_probe=extended_probe,
            skip_list_fetch=skip_list_fetch,
        )

    def get_testcase_detail(self, testcase_version_id: int):
        return self.repository.get_testcase_detail(testcase_version_id)

    def get_testcase_teststep(self, testcase_id: int):
        return self.repository.get_testcase_teststep(testcase_id)

    def try_fetch_testcase_attachments(self, item_id: int):
        return self.repository.try_fetch_testcase_attachments(item_id)

    def download_attachment_by_file_id(self, file_id: str):
        return self.repository.download_attachment_by_file_id(file_id)

    def get_attachments_for_execution(self, execution_id: int):
        return self.repository.get_attachments_for_execution(execution_id)
