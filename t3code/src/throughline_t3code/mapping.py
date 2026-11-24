"""Raw T3 Code records to Throughline messages.

Work records (projects, threads, messages) map one to one and never read the
clock. Usage records go through a small state machine kept in the cursor: it
drops unchanged snapshots, computes burn since the previous snapshot, and
emits a 100% status at every reset, including resets no snapshot has seen yet
(the window's reset time has passed).
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from .fetch import parse_moment

MAPPING_REVISION = 3
PROJECTS = "t3code.projects"
THREADS = "t3code.threads"
MESSAGES = "t3code.messages"
SUBSCRIPTIONS = "t3code.subscriptions"
USAGE = "t3code.usage"
RESETS = "t3code.resets"

# Two reset times closer than this are the same reset (providers jitter it).
RESET_TOLERANCE = timedelta(minutes=20)
# A snapshot whose used share moved less than this is not a new status.
MIN_CHANGE = 0.5
# A drop to at most EARLY_FLOOR points, at least EARLY_DROP below the cycle's
# high-water mark, with the reset time unchanged, is an early reset (a reset
# credit spent): providers keep the weekly anchor when a credit refills it.
EARLY_DROP = 25
EARLY_FLOOR = 10
# Most resets synthesized for one window in one run (two years of weekly
# windows); beyond it the window jumps to the current cycle without records.
MAX_SYNTHESIZED = 104
PROVIDER_NAMES = {
    "claudeAgent": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "grok": "Grok",
    "antigravity": "Antigravity",
}


def iso(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def moment(value):
    return parse_moment(value)


def duration_text(delta):
    seconds = max(0, int(delta.total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def owner_author(settings):
    system, kind, value = settings["owner_identifier"].split(":", 2)
    return {
        "system": system,
        "kind": kind,
        "value": value,
        "display_name": settings.get("owner_name"),
    }


def agent_author(instance):
    instance = instance or "agent"
    return {
        "system": "t3code",
        "kind": "agent",
        "value": instance,
        "display_name": PROVIDER_NAMES.get(instance, instance),
    }


ORCHESTRATOR = {
    "system": "t3code",
    "kind": "agent",
    "value": "orchestrator",
    "display_name": "Agent prompt",
}


# -- work -------------------------------------------------------------------


class Work:
    """Maps one run's work records; the first home that holds an id wins."""

    def __init__(self, settings):
        self.settings = settings
        self.environments = {}
        self.projects = {}
        self.threads = {}

    def remember(self, record):
        kind = record["kind"]
        if kind == "environment":
            self.environments.setdefault(record["home"], record["environment_id"])
        elif kind == "project":
            self.projects.setdefault(record["payload"]["id"], record)
        elif kind == "thread":
            self.threads.setdefault(record["payload"]["id"], record)

    def environment_fields(self, record):
        return dict(environment_id=record["environment_id"], environment=record["home"])

    def project_title(self, project_id):
        project = self.projects.get(project_id)
        return project["payload"]["title"] if project else None

    def thread_ref(self, thread_id):
        thread = self.threads.get(thread_id)
        payload = thread["payload"] if thread else {}
        project = self.project_title(payload.get("project_id")) or "No project"
        title = payload.get("title") or "Untitled thread"
        return {
            "external_id": thread_id,
            "title": f"{project} · {title}"[:300],
            "metadata": {
                "kind": "t3code_thread",
                "project_id": payload.get("project_id"),
                "root_thread": payload.get("root_thread_id") or thread_id,
            },
        }

    def map_project(self, record):
        p = record["payload"]
        threads = [t for t in self.threads.values() if t["payload"]["project_id"] == p["id"]]
        roots = [t for t in threads if not t["payload"].get("parent_thread_id")]
        created = moment(p["created_at"])
        return dict(
            channel=PROJECTS,
            external_id=p["id"],
            thread={"external_id": "projects", "title": "T3 Code projects"},
            happened_at=iso(created),
            author=None,
            content=(
                f"T3 Code project {p['title']} at {p['workspace_root']}"
                + (" (deleted)" if p.get("deleted_at") else "")
            ),
            fields=dict(
                project_id=p["id"],
                project=p["title"],
                workspace_root=p["workspace_root"],
                deleted=bool(p.get("deleted_at")),
                top_level_threads=len(roots),
                all_threads=len(threads),
                last_updated=iso(moment(p["updated_at"])) if p.get("updated_at") else None,
                mapping_revision=MAPPING_REVISION,
                **self.environment_fields(record),
            ),
            images=[],
            payload=record["payload"],
        )

    def map_thread(self, record):
        p = record["payload"]
        project = self.project_title(p["project_id"]) or "No project"
        sub = bool(p.get("parent_thread_id"))
        parent_title = None
        if sub and p["parent_thread_id"] in self.threads:
            parent_title = self.threads[p["parent_thread_id"]]["payload"].get("title")
        content = f"T3 Code thread \"{p['title']}\" in project {project}"
        if sub:
            content += f", a subagent thread of \"{parent_title or p['parent_thread_id']}\""
        if p.get("model"):
            content += f"; model {p['model']}"
        if p.get("branch"):
            content += f"; branch {p['branch']}"
        return dict(
            channel=THREADS,
            external_id=p["id"],
            thread={"external_id": f"project:{p['project_id']}", "title": f"{project} threads"},
            happened_at=iso(moment(p["created_at"])),
            author=None,
            content=content,
            fields=dict(
                t3_thread_id=p["id"],
                project_id=p["project_id"],
                project=project,
                title=p["title"],
                provider=p.get("provider_instance") or p.get("provider"),
                model=p.get("model"),
                created_by=p.get("created_by"),
                creation_source=p.get("creation_source"),
                branch=p.get("branch"),
                worktree_path=p.get("worktree_path"),
                is_subagent=sub,
                parent_thread=p.get("parent_thread_id"),
                root_thread=p.get("root_thread_id") or p["id"],
                relationship_to_parent=p.get("relationship_to_parent"),
                forked_from=p.get("forked_from"),
                archived=bool(p.get("archived_at")),
                deleted=bool(p.get("deleted_at")),
                last_updated=iso(moment(p["updated_at"])) if p.get("updated_at") else None,
                mapping_revision=MAPPING_REVISION,
                **self.environment_fields(record),
            ),
            images=[],
            payload=record["payload"],
        )

    def map_message(self, record):
        p = record["payload"]
        thread = self.threads.get(p["thread_id"])
        tp = thread["payload"] if thread else {}
        run = record.get("run_info") or {}
        instance = run.get("instance") or tp.get("provider_instance") or tp.get("provider")
        role = p["role"]
        if role == "assistant":
            author_kind, author = "agent", agent_author(instance)
        elif p.get("created_by") == "user":
            author_kind, author = "owner", owner_author(self.settings)
        else:
            # A user-role prompt an agent wrote (a subagent brief, a delegated
            # task, a provider-injected turn): not the owner's words.
            author_kind, author = "agent_prompt", ORCHESTRATOR
        text = (p.get("text") or "").strip()
        sub = bool(tp.get("parent_thread_id"))
        return dict(
            channel=MESSAGES,
            external_id=p["id"],
            thread=self.thread_ref(p["thread_id"]),
            happened_at=iso(moment(p["created_at"])),
            author=author,
            content=text or f"({author_kind} message without text)",
            fields=dict(
                role=role,
                author_kind=author_kind,
                project_id=tp.get("project_id"),
                project=self.project_title(tp.get("project_id")),
                t3_thread_id=p["thread_id"],
                title=tp.get("title"),
                root_thread=tp.get("root_thread_id") or p["thread_id"],
                is_subagent=sub,
                provider=instance,
                model=run.get("model") or tp.get("model"),
                run=p.get("run_id"),
                creation_source=p.get("creation_source"),
                text_length=p.get("text_length"),
                text_truncated=bool(p.get("text_truncated")),
                attachment_count=p.get("attachment_count") or 0,
                mapping_revision=MAPPING_REVISION,
                **self.environment_fields(record),
            ),
            images=[],
            payload={k: v for k, v in p.items() if k != "text"},
        )


# -- usage ------------------------------------------------------------------


def subscription_name(sub):
    if not sub:
        return None
    plan = (sub.get("payload") or {}).get("plan")
    if plan:
        return plan.replace(" Subscription", "")
    return PROVIDER_NAMES.get(sub.get("instance"), sub.get("instance"))


class Usage:
    """Usage status, resets and reset credits from snapshots, in time order.

    state (kept in the cursor):
      windows: {"<account>|<window>": {used, resets_at, observed_at,
                window_minutes, label}}
      credits: {"<account>": {available, options: {id: count}, observed_at}}
    """

    def __init__(self, state, now):
        self.state = state
        self.state.setdefault("windows", {})
        self.state.setdefault("credits", {})
        self.now = now
        self.subscriptions = {}
        self.out = {}
        self.stats = dict(snapshots=0, unchanged=0, stale=0, resets=0, early_resets=0, synthesized=0)

    # Accounts -----------------------------------------------------------

    def add_subscription(self, record):
        """The newest view of each account (homes may hold older caches)."""
        known = self.subscriptions.get(record["account"])
        checked = (record.get("payload") or {}).get("checked_at") or ""
        if known is None or checked > ((known.get("payload") or {}).get("checked_at") or ""):
            self.subscriptions[record["account"]] = record

    def resolve(self, record):
        """Account of an observation; the instance's only account when unnamed."""
        if record.get("account"):
            return record["account"]
        matches = sorted(
            a for a, s in self.subscriptions.items() if s["instance"] == record["instance"]
        )
        if len(matches) == 1:
            return matches[0]
        return f"{record['instance']}:unknown"

    def sub_for(self, account):
        return self.subscriptions.get(account)

    def names(self, account, provider, window_id, label):
        sub = self.sub_for(account)
        name = subscription_name(sub) or PROVIDER_NAMES.get(provider, provider)
        return name, label or WINDOW_FALLBACK.get(window_id, window_id)

    # Usage windows ------------------------------------------------------

    def observe(self, record):
        account = self.resolve(record)
        observed = moment(record.get("observed_at"))
        resets = moment(record.get("resets_at"))
        if observed is None or resets is None or not record.get("window_id"):
            return
        self.stats["snapshots"] += 1
        key = f"{account}|{record['window_id']}"
        prev = self.state["windows"].get(key)
        minutes = record.get("window_minutes") or (prev or {}).get("window_minutes")
        label = record.get("window_label") or (prev or {}).get("label")
        used = float(record["used_percent"])
        if prev:
            last_seen = moment(prev["observed_at"])
            if observed <= last_seen:
                self.stats["stale"] += 1
                return
            self.catch_up(key, account, record, until=observed)
            prev = self.state["windows"][key]
            prev_resets = moment(prev["resets_at"])
            same_cycle = abs(resets - prev_resets) <= RESET_TOLERANCE
            event = "observed"
            if not same_cycle:
                if resets < prev_resets - RESET_TOLERANCE:
                    # The window went back in time: an older snapshot from
                    # another source. Keep the newer cycle.
                    self.stats["stale"] += 1
                    return
                if observed < prev_resets - RESET_TOLERANCE and used < prev["used"] - MIN_CHANGE:
                    event = "early_reset"
                    self.stats["early_resets"] += 1
                    self.emit_reset(account, record, key, at=observed, next_reset=resets,
                                    event="early_reset", minutes=minutes, label=label,
                                    before=prev["used"])
                    prev = self.state["windows"][key]
                    if used < MIN_CHANGE:
                        return  # The reset record is this status.
                else:
                    # A reset time moved forward without a drop in use, or a
                    # reset no snapshot has caught up with: accept the cycle.
                    prev = dict(prev, resets_at=iso(resets), used=prev["used"])
            elif (
                prev.get("peak", prev["used"]) - used >= EARLY_DROP and used <= EARLY_FLOOR
            ):
                event = "early_reset"
                self.stats["early_resets"] += 1
                self.emit_reset(account, record, key, at=observed, next_reset=resets,
                                event="early_reset", minutes=minutes, label=label,
                                before=prev.get("peak", prev["used"]))
                prev = self.state["windows"][key]
                if used < MIN_CHANGE:
                    return
            elif abs(used - prev["used"]) < MIN_CHANGE:
                self.stats["unchanged"] += 1
                prev["observed_at"] = iso(observed)
                return
            # Snapshots from different sessions jitter around the true value:
            # only a rise above this cycle's high-water mark is new use.
            burn = max(0.0, used - prev.get("peak", prev["used"]))
            hours = (observed - moment(prev["observed_at"])).total_seconds() / 3600
        else:
            event, burn, hours = "first", None, None
        self.emit_status(account, record, key, observed, resets, used, minutes, label,
                         event=event if event != "early_reset" else "observed",
                         burn=burn, hours=hours)

    def catch_up(self, key, account, record, until):
        """Emit every reset that passed before `until` and no snapshot showed."""
        prev = self.state["windows"][key]
        minutes = prev.get("window_minutes") or 10080
        step = timedelta(minutes=minutes)
        resets = moment(prev["resets_at"])
        due = int((until - resets) / step) + 1 if resets <= until else 0
        if due > MAX_SYNTHESIZED:
            skipped = due - MAX_SYNTHESIZED
            prev["resets_at"] = iso(resets + skipped * step)
            prev["used"] = 0.0
            self.stats["skipped_resets"] = self.stats.get("skipped_resets", 0) + skipped
        for _ in range(MAX_SYNTHESIZED):
            prev = self.state["windows"][key]
            resets = moment(prev["resets_at"])
            if resets > until:
                return
            self.stats["synthesized"] += 1
            self.emit_reset(account, record, key, at=resets,
                            next_reset=resets + timedelta(minutes=minutes),
                            event="reset", minutes=minutes, label=prev.get("label"),
                            before=prev["used"])

    def finish(self):
        """Resets whose time has passed by now, for every known window."""
        for key, prev in sorted(self.state["windows"].items()):
            account, window_id = key.split("|", 1)
            record = {
                "provider": prev.get("provider"),
                "instance": prev.get("instance"),
                "window_id": window_id,
                "window_label": prev.get("label"),
                "source": "reset_time",
            }
            self.catch_up(key, account, record, until=self.now)

    def emit_reset(self, account, record, key, *, at, next_reset, event, minutes, label, before):
        self.stats["resets"] += 1
        status = self.emit_status(account, record, key, at, next_reset, 0.0, minutes, label,
                                  event=event, burn=None, hours=None, reset_from=before)
        name, window = self.names(account, record.get("provider"), record["window_id"], label)
        cycle_hours = None
        prior = self.state.setdefault("last_reset", {}).get(key)
        if prior:
            cycle_hours = round((at - moment(prior)).total_seconds() / 3600, 2)
        self.state["last_reset"][key] = iso(at)
        verb = "reset early (a reset credit or a provider reset)" if event == "early_reset" else "reset"
        content = (
            f"{name} · {window} {verb}: back to 100% left (was {100 - before:.0f}% left, "
            f"{before:.0f}% used); next reset {next_reset.strftime('%a %b %d %H:%M UTC')}"
        )
        self.put(dict(
            channel=RESETS,
            external_id=f"{account}:{record['window_id']}:{event}:{iso(at)}",
            thread={"external_id": f"resets:{account}", "title": f"{name} resets"},
            happened_at=iso(at),
            author=None,
            content=content,
            fields=dict(
                event=event,
                account=account,
                subscription=name,
                provider=record.get("provider") or (self.sub_for(account) or {}).get("provider"),
                window_id=record["window_id"],
                window_label=window,
                used_before_percent=round(before, 2),
                left_before_percent=round(100 - before, 2),
                next_reset_at=iso(next_reset),
                hours_since_previous_reset=cycle_hours,
                scheduled_reset_at=iso(at) if event == "reset" else None,
                available_credits=None,
                credit_change=None,
                mapping_revision=MAPPING_REVISION,
            ),
            images=[],
            payload={"status": status["external_id"], "source": record.get("source")},
        ))

    def emit_status(self, account, record, key, at, resets, used, minutes, label, *,
                    event, burn, hours, reset_from=None):
        minutes = minutes or 10080
        name, window = self.names(account, record.get("provider"), record["window_id"], label)
        start = resets - timedelta(minutes=minutes)
        left_time = resets - at
        elapsed = min(1.0, max(0.0, (at - start).total_seconds() / (minutes * 60)))
        left = max(0.0, 100.0 - used)
        pace = used - elapsed * 100
        projected = used / elapsed if elapsed >= 0.02 else None
        # Average daily burn of this window so far: a per-status rate over a
        # few minutes is noise.
        elapsed_days = (at - start).total_seconds() / 86400
        rate = round(used / elapsed_days, 2) if elapsed_days >= 0.25 else None
        provider = record.get("provider") or (self.sub_for(account) or {}).get("provider")
        if event in ("reset", "early_reset"):
            content = (
                f"{name} · {window}: 100% left (reset), {duration_text(left_time)} left "
                f"in the window; resets {resets.strftime('%a %b %d %H:%M UTC')}"
            )
        else:
            ahead = f"{abs(pace):.0f} pts {'ahead of' if pace > 0 else 'behind'} even burn"
            content = (
                f"{name} · {window}: {left:.0f}% left ({used:.0f}% used), "
                f"{duration_text(left_time)} left in the window; resets "
                f"{resets.strftime('%a %b %d %H:%M UTC')}; {ahead}"
            )
            if burn is not None and burn > 0:
                content += f"; +{burn:.0f}% since the previous status"
        message = dict(
            channel=USAGE,
            external_id=f"{account}:{record['window_id']}:{event if event in ('reset', 'early_reset') else 'status'}:{iso(at)}",
            thread={"external_id": f"usage:{account}:{record['window_id']}",
                    "title": f"{name} · {window} usage"},
            happened_at=iso(at),
            author=None,
            content=content,
            fields=dict(
                event=event,
                account=account,
                subscription=name,
                provider=provider,
                window_id=record["window_id"],
                window_label=window,
                window_minutes=minutes,
                used_percent=round(used, 2),
                left_percent=round(left, 2),
                resets_at=iso(resets),
                window_start=iso(start),
                time_left_hours=round(left_time.total_seconds() / 3600, 2),
                time_left=duration_text(left_time),
                window_elapsed_percent=round(elapsed * 100, 2),
                pace_points=round(pace, 2),
                projected_used_at_reset=round(projected, 1) if projected is not None else None,
                burn_since_previous=round(burn, 2) if burn is not None else None,
                hours_since_previous=round(hours, 2) if hours is not None else None,
                cycle_burn_per_day=rate,
                used_before_reset=round(reset_from, 2) if reset_from is not None else None,
                status=record.get("status"),
                plan_type=record.get("plan"),
                source=record.get("source"),
                mapping_revision=MAPPING_REVISION,
            ),
            images=[],
            payload={k: record.get(k) for k in ("source", "instance", "home")},
        )
        self.put(message)
        old = self.state["windows"].get(key) or {}
        if event in ("first", "reset", "early_reset") or "peak" not in old:
            peak = used
        else:
            peak = max(old["peak"], used)
        self.state["windows"][key] = {
            "used": used,
            "peak": peak,
            "resets_at": iso(resets),
            "observed_at": iso(at),
            "window_minutes": minutes,
            "label": label,
            "provider": provider,
            "instance": record.get("instance") or (self.state["windows"].get(key) or {}).get("instance"),
        }
        return message

    # Reset credits ------------------------------------------------------

    def credits(self, record):
        account = self.resolve(record)
        observed = moment(record.get("observed_at"))
        if observed is None or record.get("available") is None:
            return
        prev = self.state["credits"].get(account)
        available = int(record["available"])
        options = {
            o["credit_id"] or o["label"] or "credit": o
            for o in record.get("options") or []
        }
        if prev and observed <= moment(prev["observed_at"]):
            return
        name = subscription_name(self.sub_for(account)) or PROVIDER_NAMES.get(
            record.get("provider"), record.get("provider"))
        if prev is None:
            event, change = "credit_balance", 0
        elif available == prev["available"]:
            prev["observed_at"] = iso(observed)
            return
        elif available > prev["available"]:
            event, change = "credit_added", available - prev["available"]
        else:
            change = available - prev["available"]
            expiry = moment(prev.get("next_expires_at"))
            event = "credit_expired" if expiry and expiry <= observed else "credit_used"
        labels = ", ".join(
            f"{o.get('label') or cid} x{o.get('available')}" for cid, o in sorted(options.items())
        )
        words = {
            "credit_balance": f"{name}: {available} reset credit(s) available",
            "credit_added": f"{name}: {change} reset credit(s) added, {available} available",
            "credit_used": f"{name}: {-change} reset credit(s) used, {available} left",
            "credit_expired": f"{name}: {-change} reset credit(s) expired, {available} left",
        }[event]
        if labels:
            words += f" ({labels})"
        if record.get("next_expires_at"):
            words += f"; next expires {record['next_expires_at'][:10]}"
        self.put(dict(
            channel=RESETS,
            external_id=f"{account}:credits:{event}:{iso(observed)}",
            thread={"external_id": f"resets:{account}", "title": f"{name} resets"},
            happened_at=iso(observed),
            author=None,
            content=words,
            fields=dict(
                event=event,
                account=account,
                subscription=name,
                provider=record.get("provider"),
                window_id=None,
                window_label=None,
                used_before_percent=None,
                left_before_percent=None,
                next_reset_at=None,
                hours_since_previous_reset=None,
                scheduled_reset_at=None,
                available_credits=available,
                credit_change=change,
                next_credit_expires_at=record.get("next_expires_at"),
                credit_options=list(options.values()) or None,
                mapping_revision=MAPPING_REVISION,
            ),
            images=[],
            payload={"source": record.get("source")},
        ))
        self.state["credits"][account] = {
            "available": available,
            "observed_at": iso(observed),
            "next_expires_at": record.get("next_expires_at"),
        }

    # Subscriptions ------------------------------------------------------

    def subscription_records(self):
        for account, sub in sorted(self.subscriptions.items()):
            p = sub["payload"]
            name = subscription_name(sub)
            windows = {
                k.split("|", 1)[1]: v
                for k, v in self.state["windows"].items()
                if k.split("|", 1)[0] == account
            }
            credits = self.state["credits"].get(account) or {}
            parts = []
            for wid, w in sorted(windows.items()):
                resets = moment(w["resets_at"])
                parts.append(
                    f"{w.get('label') or WINDOW_FALLBACK.get(wid, wid)} {100 - w['used']:.0f}% left, "
                    f"resets {resets.strftime('%a %b %d %H:%M UTC')}"
                )
            content = f"{name} subscription ({PROVIDER_NAMES.get(sub['provider'], sub['provider'])})"
            if parts:
                content += ": " + "; ".join(parts)
            if credits:
                content += f"; {credits['available']} reset credit(s)"
            first = moment(p.get("checked_at")) or self.now
            yield dict(
                channel=SUBSCRIPTIONS,
                external_id=account,
                thread={"external_id": "subscriptions", "title": "AI subscriptions"},
                happened_at=iso(first),
                author=None,
                content=content,
                fields=dict(
                    account=account,
                    subscription=name,
                    provider=sub["provider"],
                    instance=sub["instance"],
                    plan=p.get("plan"),
                    auth_type=p.get("auth_type"),
                    enabled=p.get("enabled"),
                    usage_unavailable=p.get("usage_unavailable"),
                    windows=[
                        dict(window_id=wid, used_percent=w["used"], left_percent=round(100 - w["used"], 2),
                             resets_at=w["resets_at"], observed_at=w["observed_at"])
                        for wid, w in sorted(windows.items())
                    ] or None,
                    available_credits=credits.get("available"),
                    mapping_revision=MAPPING_REVISION,
                ),
                images=[],
                payload={"instance": sub["instance"], "plan": p.get("plan"),
                         "windows": sorted(windows), "credits": credits.get("available")},
            )

    def put(self, message):
        self.out[(message["channel"], message["external_id"])] = message


WINDOW_FALLBACK = {
    "seven_day": "Weekly",
    "seven_day_fable": "Weekly · Fable",
    "seven_day_opus": "Weekly · Opus",
    "five_hour": "5-hour",
    "primary": "Weekly",
    "secondary": "Secondary",
}


def fingerprint(settings):
    text = json.dumps([settings.get("owner_identifier"), settings.get("owner_name")])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def now_utc():
    return datetime.now(UTC)
