import html

from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings


class Milestones:
    def __init__(self, qase_service: QaseService, source_service: ZephyrEnterpriseService, logger: Logger, mappings: Mappings) -> Mappings:
        self.qase = qase_service
        self.zephyr = source_service
        self.logger = logger
        self.mappings = mappings

        self.map = {}
        self.logger.divider()
        self.i = 0

    def import_milestones(self, project) -> Mappings:
        self.logger.log(f"[{project['code']}][Milestones] Importing milestones")
        limit = 250
        offset = 0

        milestones = []
       
       
        while True:
            z_milestones = self.zephyr.get_milestones(project['zephyr_id'], limit, offset)
            milestones += z_milestones['results']
            if z_milestones['resultSize'] < limit:
                break
            offset += limit
        self.logger.log(f"[{project['code']}][Milestones] Found {len(milestones)} milestones")

        self.logger.print_status(f'[{project["code"]}] Importing milestones', self.i, len(milestones), 1)
        self.import_milestone_list(milestones, project['code'])
        
        return self.mappings
    
    def import_milestone_list(self, milestones, code, prefix = ''):
        for milestone in milestones:
            self.mappings.stats.add_entity_count(code, 'milestones', 'zephyr-enterprise')
            id = self.import_milestone(milestone, code, prefix)
            if id:
                self.mappings.stats.add_entity_count(code, 'milestones', 'qase')
                self.map[milestone['id']] = id
            self.i += 1
            self.logger.print_status(f'[{code}] Importing milestones', self.i, len(milestones), 1)
            
        self.mappings.milestones[code] = self.map
        
    def import_milestone(self, milestone, code, prefix = ''):
        if milestone:
            self.logger.log(f"[{code}][Milestones] Importing milestone {milestone['name']}")

            name = milestone['name']
            if prefix != '':
                name = '[' + prefix + '] ' + name

            description = html.unescape(milestone.get('description') or '')
            due_date = None
            end = milestone.get('endDate')
            if end is not None:
                try:
                    due_date = round(int(end) / 1000)
                except (TypeError, ValueError):
                    self.logger.log(
                        f"[{code}][Milestones] Ignoring invalid endDate for {milestone.get('name')!r}: {end!r}",
                        "warn",
                    )

            return self.qase.create_milestone(
                code,
                title=name,
                description=description,
                status='completed' if bool(milestone.get('status')) else 'active',
                due_date=due_date,
            )