import datetime
import os
import re
import sys



def read_version() -> str:
    """Version of this migration, from the VERSION file at the repo root.

    Surfaced in the log header and the end-of-run report so a customer's
    attached log answers "which version are you on?" without anyone asking.
    """
    try:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "VERSION",
        )
        with open(path, "r", encoding="utf-8") as handle:
            version = handle.read().strip()
        return version or "unknown"
    except OSError:
        return "unknown"

class Logger:
    """Level-based logger.

    Levels are additive: each includes everything above it in this list.

        error    only failures that stop or corrupt the migration
        warn     plus anything skipped, truncated or silently defaulted
        info     plus normal progress: entities migrated, counts, timings
        verbose  plus request URIs and HTTP status codes
        debug    plus request parameters and bodies

    'error' and 'warn' always reach the console, whatever the level. A
    migration that quietly drops data must not look successful in the
    terminal. Everything else goes to the console only at 'verbose' or
    above, so the default run stays readable next to the progress lines.

    Every warn and error is also recorded as an end-of-run report entry when a
    Stats object is attached (see attach_stats). The report is populated from
    the logger rather than from hand-placed calls at each skip site, so a
    warning added later becomes a report line by construction instead of
    silently going missing.
    """

    LEVELS = {'error': 0, 'warn': 1, 'info': 2, 'verbose': 3, 'debug': 4}
    _ALIASES = {'warning': 'warn', 'err': 'error', 'trace': 'debug'}
    _COLORS = {'error': '31', 'warn': '33'}
    _ICONS = {'error': '✗', 'warn': '!'}

    # Messages are conventionally prefixed "[CODE][Entity] ..." or "[Entity] ...".
    # Parsing that prefix gives the report per-project grouping for free.
    _PREFIX = re.compile(r'^\[([^\]]+)\]\s*(?:\[([^\]]+)\]\s*)?')

    def __init__(self, level: str = 'info', write_to_file: bool = True, log_dir: str = './logs', prefix: str = ''):
        self.level_name = self._normalise(level)
        self.level = self.LEVELS[self.level_name]
        self.write_to_file = write_to_file
        self.log_file = None
        self.version = read_version()
        self._stats = None

        if self.write_to_file:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f'{prefix}_zephyr_enterprise_{timestamp}.log' if prefix else f'zephyr_enterprise_{timestamp}.log'
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            self.log_file = os.path.join(log_dir, filename)
            # First line of every log: the version, so a customer's attached log
            # answers "which version are you on?" without anyone asking.
            with open(self.log_file, 'w', encoding='utf-8') as f:
                f.write(f'# qase-zephyr-enterprise-migration '
                        f'v{self.version} | started '
                        f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')

    def attach_stats(self, stats):
        """Route every warn/error into the end-of-run migration report."""
        self._stats = stats

    @classmethod
    def _normalise(cls, level) -> str:
        if level is None:
            return 'info'
        # Accept the old boolean debug flag so an existing config still runs
        if isinstance(level, bool):
            return 'debug' if level else 'info'
        name = str(level).strip().lower()
        name = cls._ALIASES.get(name, name)
        return name if name in cls.LEVELS else 'info'

    @classmethod
    def _split_prefix(cls, message: str):
        """Pull "[CODE][Entity]" or "[Entity]" off the front of a message.

        Returns (code, entity, remainder). A single bracket group is an entity,
        not a project code, because messages logged outside a project context
        look like "[Projects] ...".
        """
        match = cls._PREFIX.match(message or '')
        if not match:
            return None, None, message
        first, second = match.group(1), match.group(2)
        remainder = message[match.end():]
        if second:
            return first, second, remainder
        return None, first, remainder

    def log(self, message: str, level: str = 'info', code: str = None, entity: str = None):
        name = self._normalise(level)
        severity = self.LEVELS[name]

        if severity <= self.LEVELS['warn'] and self._stats is not None:
            parsed_code, parsed_entity, remainder = self._split_prefix(message)
            self._stats.add_issue(
                code if code is not None else parsed_code,
                entity if entity is not None else (parsed_entity or '-'),
                remainder or message,
                level=name,
            )

        if severity > self.level:
            return

        time_str = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{time_str}][{name}] {message}"

        if self.write_to_file and self.log_file:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(line + "\n")

        if severity <= self.LEVELS['warn']:
            color = self._COLORS.get(name, '0')
            icon = self._ICONS.get(name, '')
            # Leading newline so the message does not land on top of a progress
            # line, which print_status redraws with a carriage return
            print(f"\n\t\033[{color}m{icon}\033[0m {line}", file=sys.stderr, flush=True)
        elif self.level >= self.LEVELS['verbose']:
            print(line, flush=True)

    def divider(self):
        self.log('-----------------------------------')

    def print_status(self, message: str, completed: int = 0, total: int = 0, level: int = 0):
        icon = '↪'
        color_code = '34'
        if completed != 0 and total != 0:
            message = f"{message} [{completed}/{total}]"
        if completed == total:
            icon = '✓'
            color_code = '32'

        tabs = '\t'
        for i in range(level):
            tabs += '  '
        print(f"{tabs}\033[{color_code}m{icon}\033[0m {message}", end='\r', flush=True)
        if completed == total:
            print()

    def print_group(self, message: str):
        print(f"\t\033[35m↪\033[0m {message}", end='\r')
        print()
