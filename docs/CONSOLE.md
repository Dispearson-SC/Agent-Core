# The operator console

How to bring the database up the first time, how to start the console, and what every
command in it is for.

`README.md` is the runbook — it owns the environment, the preflight and the reasoning
behind each decision. This file owns the console itself: the commands, and what each one
is actually *for*. Where the two would overlap, this one points rather than repeats.

---

## 1. Where to run it

**Anywhere.** The package is installed, so `python -m agent_core` resolves from any
directory, and `.env` is located relative to the repository rather than to your working
directory. There is no `cd` you have to remember.

```
python -m agent_core preflight    # what is not ready, all of it, in one pass
python -m agent_core console      # the operator REPL
python -m agent_core serve        # the HTTP process
python -m agent_core peer-worker  # answers one agent's questions to another
```

**Run `preflight` first, every time.** It answers *what is not ready* in a single pass —
databases, schema, policy rules, every profile's servability, credentials by name, the
model endpoint — and it exits non-zero when something is genuinely broken. It exists
because every failure in this system used to surface one at a time, three commands apart.

---

## 2. Bringing the database up the first time

PostgreSQL has to be running. Nothing else does.

The application needs two databases: the one it stores turns in, and the one the durable
engine keeps its own state in. **It will not create them itself unless you let it**, and
that is deliberate: creating a database needs privileges an application should not hold
while it merely runs.

### The one-command way — development

Put an administrative connection string in `.env` and start once:

```
AGENT_CORE_ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
```

Startup creates both databases, applies every migration and loads the policy rules. It is
**idempotent** — starting again changes nothing — so you can leave it set while you are
developing.

### The explicit way — production

Create them once, with a role that may, and never give the application that role:

```sql
CREATE DATABASE "agent_core_app";
CREATE DATABASE "agent_core_app_dbos_sys";
```

Then start normally. Migrations still apply on every start, still idempotently.

**In production, set the admin URL for one bootstrap run and remove it.** The application
never needs superuser to RUN — only to bootstrap — and a deployment that leaves those
credentials in its environment has handed away a privilege it only needed once.

### If something is missing

You will not have to guess. A missing database fails with the host, the port, both
`CREATE DATABASE` statements and the admin-URL alternative, and `preflight` reports the
same thing before anything starts.

---

## 3. The two modes, and why you get to choose

```
python -m agent_core console                    # direct  (default)
python -m agent_core console --durable          # durable
```

Inside the console, `:mode durable` and `:mode direct` switch at any time. The prompt
carries `[direct]` or `[durable]` on **every** line, so the mode is never something you
have to remember.

**Direct** calls the use case in this process and answers immediately. That is what makes
an agent worth iterating on. It is real proof of the profile, the tool provider, the policy
engine and the audit sink — those are the exact collaborators this deployment was built
with — and it proves **nothing** about three things, which it tells you at startup:

- **durability** — a crash mid-turn is not recovered and not replayed
- **coalescing** — the per-session queue and its debounce window never run
- **publication** — a suspension raised here has no correlation handle a real channel
  could hand back

It also never reaches the workflow step that hands a question to a peer, so **an
`ask_peer` call suspends and no peer is ever actually asked.**

**Durable** enqueues through the DBOS workflow and polls for the result — the same two
seats `POST /turns` and `GET /turns/{turn_id}` use. Slower to answer, and the only mode in
which delegation completes.

---

## 4. The commands

### Moving between agents

| Command | What it is for |
|---|---|
| `:agents` | Every loaded profile, with the version a turn would record. |
| `:use <id>` | Switch agent. Prints what changed — persona, model, toolsets, approval rules — because the policy surface changes with it. |
| `:new <id>` | Scaffold a profile from a template worth reading, then `:reload`. |
| `:reload` | Re-read the profiles directory. **A file that fails to load changes nothing** — the agent in force stays in force, and you are told. |

### Seeing what an agent actually is

| Command | What it is for |
|---|---|
| `:profile` | The **resolved** configuration — not the raw YAML. What the system made of the file. |
| `:tools` | The toolset this profile gets, and what policy does to each name. **The most useful command here.** |
| `:policy [tool]` | What the engine decides for *this* caller, and which rule wins. |

`:tools` is worth reading closely. It prints each tool with its effect and the `rule_id`
that produced it, then a line like:

```
advertised to the model: 4 of 5 (a deny tool is never shown to it)
```

That last clause is the design: the defence is not filtering what the model asks for, it is
that a refused tool **is not on the object the model holds**. A prompt injection cannot
call a method that is not there.

### Reading what happened

| Command | What it is for |
|---|---|
| `:audit` | The tool calls of the last turn, whole — arguments, verdict, and the sentence the winning rule gave. |
| `:trace [user\|admin]` | The last turn as the transcript projects it, from either side. |

`:trace user` is the one to reach for when you are deciding whether something is safe to
ship: it shows you **exactly what a user would have been served**. A user must see *that*
something is pending and never *which* tool — this is how you check that by reading rather
than by trusting.

### Approvals

| Command | What it is for |
|---|---|
| `:pending` | What the last turn is waiting on a human for. |
| `:approve <id>` | Answer YES. |
| `:refuse <id>` | Answer NO — never blocked. |

**`:approve` will usually refuse, and that is correct.** The four-eyes rule says the
approver may not be the requester, and in a one-operator console they are the same person.
It says so by name rather than failing quietly. `:refuse` is never blocked: refusing your
own request is always allowed, because the risk a second pair of eyes exists to catch is
someone approving their own action, not someone stopping it.

### Conversations

| Command | What it is for |
|---|---|
| `:sessions` | Conversations in this tenant; the ones stuck on a human are marked. |
| `:resume <id>` | File the next turns under that session instead of this run's fresh one. |

`:resume` is how you test compaction. A fresh session cannot — the ladder needs a long
conversation before it has anything to compact.

### The rest

| Command | What it is for |
|---|---|
| `:mode [direct\|durable]` | Which path a turn takes, and what that path does not run. No argument reports the current mode. |
| `:help` | The list. |
| `:quit` | Leave. |
| *anything else* | A turn. It goes to the current agent. |

---

## 5. One agent asking another

Delegation needs **two processes**, and the second one is easy to forget:

```
python -m agent_core peer-worker                         # one terminal
python -m agent_core console --durable --role operator   # another
```

Then `:use support_triage` and ask something only `billing_specialist` can answer — the two
shipped profiles already name each other.

Three things have to be true, and each will tell you by name if it is not:

- **`peer-worker` running.** When one agent asks another, the question lands on a durable
  queue and the asking turn suspends — it does not block. Something has to claim that
  question, run a turn *as the answering agent*, and post the reply. Without it the ask is
  queued, correct, and answered by nobody.
- **`--durable`.** The step that hands the question to the mailbox lives in the workflow.
- **`--role operator`.** The grant in `Core/policy/rules.yaml` names both a role and a
  channel. Without the role it does not match and the delegation is refused with
  `[no matching rule]` — **which is the policy engine working**, not a misconfiguration.

The answering turn runs under the target agent's own profile, toolset, policy and budget —
never the asker's. Running it as the asker would make delegation a privilege escalation
dressed as a question, and an invisible one: the audit row would name the asker and
everything would look correct.

---

## 6. Adding to the system

One line decides whether something is code or configuration:

> **A tool is behaviour, so adding one is code.** Everything else — which tools an agent may
> use, which MCP server it borrows from, which agent it may ask, what needs approval — is a
> YAML file.

`README.md` §5 has the full table. The short version: a new tool is a Python function
registered once; a new agent, a new MCP connection and a new peer relationship are all
edits to `Core/profiles/*.yaml`; and what needs a human is `Core/policy/*.yaml`, reconciled
into the policy table at every startup.

**Policy is deliberately not a console command.** A permission granted at a prompt is a
permission granted with no record.
