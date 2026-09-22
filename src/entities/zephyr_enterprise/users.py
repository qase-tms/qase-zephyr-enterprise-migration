import asyncio
import sys
from collections import Counter, defaultdict

from ...service import QaseService, QaseScimService, ZephyrEnterpriseService
from ...support import Logger, Mappings, ConfigManager as Config, Pools


def _unwrap_zephyr_user_list(raw) -> list:
    """Normalize ``GET user/filter`` JSON to a list of user dicts."""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for k in ("data", "users", "entities", "results", "items", "userDTOs"):
            v = raw.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _zephyr_login_key(zephyr_user: dict) -> str:
    """Email for matching; if Zephyr omits ``email``, use ``username`` (SCIM ``userName``)."""
    e = (zephyr_user.get("email") or "").strip()
    if e:
        return e
    return (zephyr_user.get("username") or "").strip()


def _collect_zephyr_groups_from_user_group_sets(zephyr_users: list) -> list:
    """Build group specs from ``groupSet`` on each user (``GET user/filter``).

    Each item: ``{zephyr_group_id, qase_display_name, zephyr_user_ids}``.
    """
    acc: dict = defaultdict(set)
    for u in zephyr_users or []:
        if not isinstance(u, dict):
            continue
        try:
            zuid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        for g in u.get("groupSet") or []:
            if not isinstance(g, dict):
                continue
            if g.get("disabled"):
                continue
            try:
                gid_i = int(g.get("id")) if g.get("id") is not None else 0
            except (TypeError, ValueError):
                gid_i = 0
            gname = (g.get("name") or "").strip() or (f"group-{gid_i}" if gid_i else "group")
            acc[(gid_i, gname)].add(zuid)

    if not acc:
        return []

    name_lc_counts = Counter()
    for (_gid, gname) in acc:
        name_lc_counts[gname.lower()] += 1

    out = []
    for (gid_i, gname), uids in sorted(acc.items(), key=lambda kv: (kv[0][1].lower(), kv[0][0])):
        display = gname
        if name_lc_counts[gname.lower()] > 1 and gid_i:
            display = f"{gname} (Zephyr group id {gid_i})"
        out.append(
            {
                "zephyr_group_id": gid_i,
                "qase_display_name": display,
                "zephyr_user_ids": sorted(uids),
            }
        )
    return out


class Users:
    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrEnterpriseService,
        logger: Logger,
        mappings: Mappings,
        config: Config,
        pools: Pools,
        scim_service: QaseScimService = None,
    ):
        self.qase = qase_service
        self.scim = scim_service
        self.zephyr = source_service
        self.logger = logger
        self.mappings = mappings
        self.config = config
        self.pools = pools
        self.map = {}  # Zephyr user id → Qase user id. Used for mapping users to groups
        self.active_ids = []  # This is a list of Qase active users that should be added to groups
        self.zephyr_users = []
        self._existing_groups = None  # {display_name_lower: group_id}, lazy-loaded for dedup
        self.logger.divider()

    def import_users(self):
        return asyncio.run(self.import_users_async())
    
    async def import_users_async(self):
        await self.get_zephyr_users()

        if self.scim is not None:
            await self.create_users()

        await self.build_map()

        if self.scim is not None and self.config.get("groups.create"):
            await self.migrate_groups_to_qase()

        return self.mappings

    async def build_map(self):
        self.logger.log("[Users] Building users map")
        qase_users = await self.pools.qs_gen_all(self.qase.get_all_users)
        self.mappings.stats.add_user('qase', len(qase_users))
        self.mappings.stats.add_user('zephyr-enterprise', len(self.zephyr_users))
        i = 0
        total = len(self.zephyr_users)
        self.logger.print_status('Building users map', i, total)
        for zephyr_user in self.zephyr_users:
            i += 1
            zu_key = _zephyr_login_key(zephyr_user)
            if not zu_key:
                self.logger.log(
                    f"[Users] Zephyr user id={zephyr_user.get('id')} has no email or username; using default user.",
                    "warn",
                )
                self.mappings.users[zephyr_user["id"]] = self.mappings.default_user
                self.logger.print_status("Building users map", i, total)
                continue
            mapped = (self.config.get("users.map") or {}).get(zu_key)
            if mapped is not None:
                self.mappings.users[zephyr_user["id"]] = mapped
                self.logger.print_status('Building users map', i, total)
                continue
            flag = False
            for qase_user in qase_users:
                qase_user = qase_user.to_dict()
                if zu_key.lower() == (qase_user.get("email") or "").lower():
                    self.mappings.users[zephyr_user['id']] = qase_user['id']
                    flag = True
                    self.logger.log(f"[Users] User {zu_key} found in Qase as {qase_user['email']}")
                    break
            if not flag:
                # Not found, using default user
                self.mappings.users[zephyr_user['id']] = self.mappings.default_user
                self.logger.log(f"[Users] User {zu_key} not found in Qase, using default user.")
            self.logger.print_status('Building users map', i, total)

    async def create_users(self):
        print("[Users] Loading users from Qase (SCIM)...", flush=True)
        self.logger.log("[Users] Loading users from Qase using SCIM")
        all_qase_users = await self.pools.qs_gen_all(self.scim.get_all_users)
        if not isinstance(all_qase_users, list):
            all_qase_users = (
                list(all_qase_users)
                if hasattr(all_qase_users, "__iter__") and not isinstance(all_qase_users, (str, bytes))
                else [all_qase_users]
            )
        flattened = []
        for x in all_qase_users:
            if isinstance(x, list):
                flattened.extend(x)
            else:
                flattened.append(x)
        all_qase_users = flattened

        users_to_create = []
        for zephyr_user in self.zephyr_users:
            zu_key = _zephyr_login_key(zephyr_user)
            if not zu_key:
                self.logger.log(
                    f"[Users] Zephyr user id={zephyr_user.get('id')} has no email or username; skipping SCIM step.",
                    "warn",
                )
                continue
            flag = False
            for qase_user in all_qase_users:
                qase_email = (
                    qase_user.get("userName", "").lower()
                    if isinstance(qase_user, dict)
                    else getattr(qase_user, "userName", "").lower()
                )
                if zu_key.lower() == qase_email:
                    self.logger.log("[Users] User found in Qase using SCIM, skipping creation.")
                    qase_user_id = (
                        qase_user.get("id")
                        if isinstance(qase_user, dict)
                        else getattr(qase_user, "id", None)
                    )
                    self.map[zephyr_user["id"]] = qase_user_id
                    if zephyr_user.get("accountEnabled", True):
                        self.active_ids.append(qase_user_id)
                    flag = True
                    break
            if not flag:
                if zephyr_user.get("accountEnabled", True) is False and self.config.get("users.only_active", True):
                    self.logger.log(f"[Users] User {zu_key} is not active, skipping creation.")
                    continue
                users_to_create.append(zephyr_user)

        if self.config.get("users.create") and len(users_to_create) > 0:
            self._display_user_creation_summary(users_to_create, all_qase_users)
            sys.stdout.flush()
            sys.stderr.flush()
            confirmation = input("\n[Users] Type 'yes' to proceed with user creation: ").strip().lower()
            if confirmation != "yes":
                self.logger.log("[Users] User creation cancelled by user. Skipping user creation.")
                return

        if self.config.get("users.create") and len(users_to_create) > 0:
            async with asyncio.TaskGroup() as tg:
                for zephyr_user in users_to_create:
                    try:
                        tg.create_task(self.import_user(zephyr_user))
                    except Exception as e:
                        zu_key = _zephyr_login_key(zephyr_user)
                        self.logger.log(f"[Users] Failed to create user {zu_key}", "error")
                        self.logger.log(f"{e}")

    def _display_user_creation_summary(self, users_to_create, qase_users):
        """Summarize planned SCIM user creates, to console and log."""

        def out(msg):
            print(msg, flush=True)
            self.logger.log(msg.strip())

        out("")
        out("=" * 80)
        out("USER CREATION SUMMARY")
        out("=" * 80)

        zephyr_host = self.config.get("zephyr.host") or "N/A"
        out(f"\nSource Zephyr Enterprise URL: {zephyr_host}")

        out(f"\nExisting Qase users (SCIM) ({len(qase_users)}):")
        if len(qase_users) > 0:
            out("-" * 80)

            def _scim_sort_key(u):
                if isinstance(u, dict):
                    return (u.get("userName") or "").lower()
                return getattr(u, "userName", "").lower()

            sorted_users = sorted(qase_users, key=_scim_sort_key)
            for i, qase_user in enumerate(sorted_users[:50], 1):
                if isinstance(qase_user, dict):
                    email = qase_user.get("userName", "N/A")
                    name = qase_user.get("name", {})
                    if isinstance(name, dict):
                        full_name = f"{name.get('givenName', '')} {name.get('familyName', '')}".strip() or "N/A"
                    else:
                        full_name = str(name) if name else "N/A"
                    active = qase_user.get("active", True)
                    status = "Active" if active else "Inactive"
                    out(f"  {i}. {full_name} ({email}) — {status}")
                else:
                    email = getattr(qase_user, "userName", "N/A")
                    name = getattr(qase_user, "name", None)
                    if name:
                        full_name = (
                            f"{getattr(name, 'givenName', '')} {getattr(name, 'familyName', '')}".strip() or "N/A"
                        )
                    else:
                        full_name = "N/A"
                    active = getattr(qase_user, "active", True)
                    status = "Active" if active else "Inactive"
                    out(f"  {i}. {full_name} ({email}) — {status}")
            if len(qase_users) > 50:
                out(f"  ... and {len(qase_users) - 50} more users")
            out("-" * 80)
        else:
            out("  No existing users returned by SCIM")
            out("-" * 80)

        out(f"\nUsers to be created in Qase: {len(users_to_create)}")
        out("-" * 80)

        active_users = [u for u in users_to_create if u.get("accountEnabled", True)]
        inactive_users = [u for u in users_to_create if not u.get("accountEnabled", True)]

        if active_users:
            out(f"\nActive ({len(active_users)}):")
            for i, user in enumerate(active_users, 1):
                zu_key = _zephyr_login_key(user)
                title = user.get("title") or ""
                fn = user.get("firstName") or ""
                ln = user.get("lastName") or ""
                out(f"  {i}. {fn} {ln}".strip() + f" ({zu_key}) — title: {title or 'N/A'}")

        if inactive_users:
            out(f"\nInactive ({len(inactive_users)}):")
            for i, user in enumerate(inactive_users, 1):
                zu_key = _zephyr_login_key(user)
                title = user.get("title") or ""
                fn = user.get("firstName") or ""
                ln = user.get("lastName") or ""
                out(f"  {i}. {fn} {ln}".strip() + f" ({zu_key}) — title: {title or 'N/A'} [INACTIVE]")

        out("-" * 80)
        out(f"Total: {len(users_to_create)} users")
        out("=" * 80)
        out("")

    async def import_user(self, zephyr_user):
        user_id = await self.create_user(zephyr_user)
        self.map[zephyr_user["id"]] = user_id
        if zephyr_user.get("accountEnabled", True):
            self.active_ids.append(user_id)

    async def create_user(self, zephyr_user):
        zu_key = _zephyr_login_key(zephyr_user)
        self.logger.log(f"[Users] Creating user {zu_key} in Qase")

        user_id = await self.pools.qs(
            self.scim.create_user,
            zu_key,
            zephyr_user.get("firstName") or "",
            zephyr_user.get("lastName") or "",
            zephyr_user.get("title") or "",
            int(bool(zephyr_user.get("accountEnabled", True))),
        )
        self.logger.log(f"[Users] User {zu_key} created in Qase with id {user_id}")
        return user_id

    async def get_zephyr_users(self):
        self.logger.log("[Users] Getting users from Zephyr Enterprise")
        raw = await self.pools.source(self.zephyr.get_users)
        self.zephyr_users = _unwrap_zephyr_user_list(raw)
        self.logger.log(f"[Users] Parsed {len(self.zephyr_users)} Zephyr user row(s) from user/filter")
        return self.zephyr_users

    async def _load_existing_qase_groups(self) -> dict:
        """Return ``{display_name_lower: group_id}`` for groups already in Qase (SCIM).

        Used to avoid recreating groups that already exist (idempotent re-runs).
        On the first matching name we keep its id; later duplicates are ignored.
        """
        raw = await self.pools.qs_gen_all(self.scim.get_all_groups)
        groups = []
        for x in (raw if isinstance(raw, list) else [raw]):
            if isinstance(x, list):
                groups.extend(x)
            else:
                groups.append(x)

        existing: dict = {}
        for g in groups:
            name = (
                g.get("displayName")
                if isinstance(g, dict)
                else getattr(g, "displayName", None)
            )
            gid = g.get("id") if isinstance(g, dict) else getattr(g, "id", None)
            if not name or gid is None:
                continue
            key = name.strip().lower()
            if key and key not in existing:
                existing[key] = gid
        return existing

    async def _get_or_create_group(self, name: str):
        """Reuse an existing Qase group with the same display name, else create it.

        Matching is case-insensitive on display name. Newly created groups are
        cached so duplicate names within a single run are also deduplicated.
        """
        if getattr(self, "_existing_groups", None) is None:
            self._existing_groups = await self._load_existing_qase_groups()

        key = (name or "").strip().lower()
        existing_id = self._existing_groups.get(key)
        if existing_id is not None:
            self.logger.log(
                f"[Users][Groups] Group {name!r} already exists (id={existing_id}); skipping creation."
            )
            return existing_id

        qgid = await self.pools.qs(self.scim.create_group, name)
        if key:
            self._existing_groups[key] = qgid
        self.logger.log(f"[Users][Groups] Created Qase group {name!r} id={qgid}")
        return qgid

    async def migrate_groups_to_qase(self):
        """Create Qase groups via SCIM from Zephyr ``groupSet``, or a single root group."""
        self.logger.log("[Users][Groups] SCIM group migration enabled")
        self._existing_groups = await self._load_existing_qase_groups()
        self.logger.log(
            f"[Users][Groups] Found {len(self._existing_groups)} existing group name(s) in Qase"
        )
        prefix = (self.config.get("groups.name_prefix") or "").strip()

        specs = []
        if self.config.get("groups.from_user_group_sets", True):
            specs = _collect_zephyr_groups_from_user_group_sets(self.zephyr_users)
        if specs:
            self.logger.log(
                f"[Users][Groups] Creating {len(specs)} group(s) from Zephyr user groupSet"
            )
            await self._import_groups_from_specs(specs, prefix)
            return

        if self.config.get("groups.from_user_group_sets", True):
            self.logger.log(
                "[Users][Groups] No groupSet on users; falling back to groups.name if set "
                "(see README: Zephyr groups).",
                "warn",
            )

        root = (self.config.get("groups.name") or "").strip()
        if root:
            await self.create_root_group(root)
        else:
            self.logger.log(
                "[Users][Groups] Set groups.name for a single root group, or populate groupSet on users.",
                "warn",
            )

    async def _import_groups_from_specs(self, specs: list, name_prefix: str) -> None:
        first_set = False
        for spec in specs:
            raw = (spec.get("qase_display_name") or "group").strip()
            gname = f"{name_prefix}{raw}" if name_prefix else raw
            if len(gname) > 200:
                gname = gname[:197] + "..."

            qase_ids = []
            for zid in spec.get("zephyr_user_ids") or []:
                qid = self.mappings.users.get(zid)
                if qid is not None:
                    qase_ids.append(int(qid))
            if not qase_ids:
                self.logger.log(
                    f"[Users][Groups] Skipping {gname!r}: no Zephyr users mapped to Qase authors",
                    "warn",
                )
                continue
            try:
                qgid = await self._get_or_create_group(gname)
            except Exception as e:
                self.logger.log(f"[Users][Groups] create_group failed for {gname!r}: {e!r}", "error")
                continue
            if not first_set:
                self.mappings.group_id = qgid
                first_set = True
            for quid in qase_ids:
                try:
                    await self.pools.qs(self.scim.add_user_to_group, qgid, quid)
                except Exception as e:
                    self.logger.log(
                        f"[Users][Groups] add_user_to_group failed group={qgid} user={quid}: {e!r}",
                        "warn",
                    )

    async def create_root_group(self, group_name: str = None):
        """One SCIM group and all users from ``active_ids`` (users matched/created via SCIM earlier)."""
        name = (group_name or self.config.get("groups.name") or "Zephyr Migration").strip()
        self.logger.log(f"[Users][Groups] Creating root group {name!r}")
        try:
            self.mappings.group_id = await self._get_or_create_group(name)
        except Exception as e:
            self.logger.log(f"[Users][Groups] create_group failed: {e!r}", "error")
            return
        for uid in self.active_ids:
            self.logger.log(f"[Users][Groups] Adding user {uid} to group {name!r}")
            try:
                await self.pools.qs(self.scim.add_user_to_group, self.mappings.group_id, uid)
            except Exception as e:
                self.logger.log(
                    f"[Users][Groups] add_user_to_group failed user={uid}: {e!r}",
                    "warn",
                )
