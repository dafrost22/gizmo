"""Deploy and operate Gizmo's AgentCore brain (direct code deploy: a zip in S3, no containers or CDK).

    python deploy.py deploy [--profile admin] [--boundary my-boundary]   build, upload, role, create/update
    python deploy.py status                                              the runtime and its ARN
    python deploy.py ask "Where is the space station?" ["And then?" ...] invoke it the way the robot does
    python deploy.py logs [--minutes 30]                                 recent CloudWatch logs
    python deploy.py delete                                              the runtime and its role (not the bucket)

--profile deploys (it needs IAM, S3 and bedrock-agentcore-control rights); --invoke-profile is what `ask`
invokes as, e.g. the robot's own credentials (bedrock-agentcore:InvokeAgentRuntime only). The robot's
console takes the ARN that deploy prints (Settings, "What thinks"). The runtime's code is app.py plus copies of
the robot's demo MCP server (../robot/gizmo/demo_server) and of its host's side of skills over MCP
(../robot/gizmo/mcp_skills.py): the runtime lists and loads the skills its servers serve itself.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import boto3
import botocore.exceptions

HERE = Path(__file__).resolve().parent
ROBOT = HERE.parent / "robot" / "gizmo"
BUILD_DIR = HERE / ".build"
KEEP_ZIPS = 5
MAX_ZIP_MB, MAX_UNZIPPED_MB = 250, 750
ASK_PROMPT = ("You are Gizmo, a small Reachy Mini robot. A voice model speaks your words aloud: one to three short "
              "spoken sentences, no markdown. Load a skill when a question is about something it covers, and use "
              "your tools for live facts.")


def say(msg: str, end: str = "\n") -> None:
    print(msg, end=end, flush=True)


def session(args, profile: str | None = None) -> boto3.Session:
    return boto3.Session(profile_name=profile or args.profile, region_name=args.region)


# --- build --------------------------------------------------------------------------------------------

def build(args) -> Path:
    """linux/arm64 wheels plus app.py, the robot's demo MCP server and its skills host, in one zip (AgentCore
    runs arm64)."""
    out = BUILD_DIR / args.name
    shutil.rmtree(out, ignore_errors=True)
    pkg = out / "pkg"
    pkg.mkdir(parents=True)
    say("Installing arm64 dependencies…")
    subprocess.run(["uv", "pip", "install", "-q", "--python-platform", "aarch64-manylinux2014", "--python-version", "3.12",
                    "--target", str(pkg), "--only-binary=:all:", "-r", "pyproject.toml"], cwd=HERE, check=True)
    shutil.copy2(HERE / "app.py", pkg / "app.py")
    shutil.copytree(ROBOT / "demo_server", pkg / "demo_server")  # it imports nothing else from the robot
    shutil.copy2(ROBOT / "mcp_skills.py", pkg / "mcp_skills.py")  # nor does this: the host's side of skills over MCP
    for cache in pkg.rglob("__pycache__"):
        shutil.rmtree(cache)
    zip_path = out / f"{args.name}.zip"
    unzipped = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(pkg.rglob("*")):
            if path.is_dir() or path.suffix == ".pyc":
                continue
            info = zipfile.ZipInfo.from_file(path, path.relative_to(pkg).as_posix())
            info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16  # AgentCore needs 644/755
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
            unzipped += path.stat().st_size
    zipped_mb, unzipped_mb = zip_path.stat().st_size / 1e6, unzipped / 1e6
    say(f"  {zip_path.name}: {zipped_mb:.1f} MB zipped, {unzipped_mb:.1f} MB unzipped")
    if zipped_mb > MAX_ZIP_MB or unzipped_mb > MAX_UNZIPPED_MB:
        sys.exit(f"Package too big for direct code deploy ({MAX_ZIP_MB} MB zipped / {MAX_UNZIPPED_MB} MB unzipped)")
    return zip_path


# --- AWS pieces ---------------------------------------------------------------------------------------

def ensure_bucket(s3, bucket: str, account: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
        return
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
    say(f"Creating bucket {bucket} (private, encrypted)")
    s3.create_bucket(Bucket=bucket, **({} if region == "us-east-1" else
                                       {"CreateBucketConfiguration": {"LocationConstraint": region}}))
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})


def upload(s3, bucket: str, account: str, prefix: str, zip_path: Path) -> str:
    key = f"{prefix}/{zip_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    say(f"Uploading s3://{bucket}/{key}")
    s3.upload_file(str(zip_path), bucket, key, ExtraArgs={"ExpectedBucketOwner": account})
    old = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/", ExpectedBucketOwner=account).get("Contents", [])
    for obj in sorted(old, key=lambda o: o["LastModified"], reverse=True)[KEEP_ZIPS:]:
        s3.delete_object(Bucket=bucket, Key=obj["Key"], ExpectedBucketOwner=account)
    return key


def runtime_policy(args, account: str, bucket: str) -> dict:
    """Everything the runtime itself needs: Claude on Bedrock, its logs and traces, its code."""
    r = args.region
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Claude", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": ["arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
                          f"arn:aws:bedrock:*:{account}:inference-profile/us.anthropic.claude-*",
                          f"arn:aws:bedrock:*:{account}:inference-profile/global.anthropic.claude-*"]},
            {"Sid": "Logs", "Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
             "Resource": [f"arn:aws:logs:{r}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                          f"arn:aws:logs:{r}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:*"]},
            {"Sid": "DescribeLogGroups", "Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": "*"},
            {"Sid": "Telemetry", "Effect": "Allow",
             "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"],
             "Resource": "*"},
            {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
             "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
            {"Sid": "Code", "Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
             "Resource": f"arn:aws:s3:::{bucket}/{args.code_prefix}/*"},
            {"Sid": "WorkloadIdentity", "Effect": "Allow",
             "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                        "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
             "Resource": [f"arn:aws:bedrock-agentcore:{r}:{account}:workload-identity-directory/default",
                          f"arn:aws:bedrock-agentcore:{r}:{account}:workload-identity-directory/default/workload-identity/{args.name}-*"]},
        ],
    }


def ensure_role(iam, args, account: str, bucket: str) -> tuple[str, bool]:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{args.region}:{account}:*"}}}]}
    created = False
    try:
        arn = iam.get_role(RoleName=args.role)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        say(f"Creating role {args.role}" + (f" (boundary {args.boundary})" if args.boundary else ""))
        arn = iam.create_role(
            RoleName=args.role, AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"AgentCore execution role for {args.name}",
            Tags=[{"Key": "project", "Value": "gizmo"}],
            **({"PermissionsBoundary": f"arn:aws:iam::{account}:policy/{args.boundary}"} if args.boundary else {}),
        )["Role"]["Arn"]
        created = True
    iam.put_role_policy(RoleName=args.role, PolicyName="runtime",
                        PolicyDocument=json.dumps(runtime_policy(args, account, bucket)))
    return arn, created


def find_runtime(ctl, name: str) -> dict | None:
    token = None
    while True:
        page = ctl.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == name:
                return rt
        token = page.get("nextToken")
        if not token:
            return None


def wait_ready(ctl, runtime_id: str) -> dict:
    for _ in range(120):
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        if rt["status"] == "READY":
            return rt
        if rt["status"].endswith("FAILED"):
            sys.exit(f"Runtime {rt['status']}: {rt.get('failureReason', 'no reason given')}")
        time.sleep(5)
    sys.exit("Runtime didn't become READY within 10 minutes")


def runtime_arn(args) -> str:
    if args.arn:
        return args.arn
    rt = find_runtime(session(args).client("bedrock-agentcore-control"), args.name)
    if not rt:
        sys.exit(f"{args.name} isn't deployed: run python deploy.py deploy")
    return rt["agentRuntimeArn"]


# --- commands -----------------------------------------------------------------------------------------

def cmd_deploy(args) -> None:
    aws = session(args)
    account = aws.client("sts").get_caller_identity()["Account"]
    bucket = args.bucket or f"gizmo-artifacts-{account}"
    zip_path = build(args)
    s3 = aws.client("s3")
    ensure_bucket(s3, bucket, account, args.region)
    key = upload(s3, bucket, account, args.code_prefix, zip_path)
    role_arn, role_created = ensure_role(aws.client("iam"), args, account, bucket)
    env = {"GIZMO_MCP_SERVERS": json.dumps(json.loads((HERE / "mcp.json").read_text()))}
    for item in args.env or []:  # e.g. EXPLORER_LAT=28.5 (the sky is otherwise seen from the AWS region)
        k, _, v = item.partition("=")
        env[k] = v
    spec = {
        "agentRuntimeArtifact": {"codeConfiguration": {
            "code": {"s3": {"bucket": bucket, "prefix": key}}, "runtime": "PYTHON_3_12", "entryPoint": ["app.py"]}},
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},  # the MCP servers and the explorer's sources are public
        "environmentVariables": env,
        "description": "Gizmo's Strands brain: skills and MCP tools, streamed back to the robot",
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
    }
    ctl = aws.client("bedrock-agentcore-control")
    existing = find_runtime(ctl, args.name)
    for attempt in range(12):  # a new role takes a few seconds to become assumable everywhere
        try:
            if existing:
                say(f"Updating runtime {args.name}")
                rt = ctl.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **spec)
            else:
                say(f"Creating runtime {args.name}")
                rt = ctl.create_agent_runtime(agentRuntimeName=args.name, tags={"project": "gizmo"}, **spec)
            break
        except botocore.exceptions.ClientError as e:
            if role_created and e.response["Error"]["Code"] in ("ValidationException", "AccessDeniedException") and attempt < 11:
                say(f"  role not usable yet ({e.response['Error']['Message'][:90]}…); retrying")
                time.sleep(5)
                continue
            raise
    rt = wait_ready(ctl, rt["agentRuntimeId"])
    say(f"Ready: {rt['agentRuntimeArn']} (version {rt.get('agentRuntimeVersion')})")
    say("Put that ARN in the robot's console (Settings, What thinks) to let Gizmo think on AgentCore.")


def cmd_status(args) -> None:
    rt = find_runtime(session(args).client("bedrock-agentcore-control"), args.name)
    say(f"{rt['agentRuntimeName']}  {rt['status']}  v{rt.get('agentRuntimeVersion')}  {rt['agentRuntimeArn']}"
        if rt else f"{args.name} isn't deployed")


def cmd_ask(args) -> None:
    arn = runtime_arn(args)
    client = session(args, args.invoke_profile).client("bedrock-agentcore")
    # Each new session id boots a fresh microVM (several seconds); reusing one keeps it warm, as the robot does.
    session_id = args.session or f"gizmo-ask-{uuid.uuid4().hex}"
    if len(session_id) < 33:
        sys.exit("--session must be at least 33 characters")
    say(f"session {session_id}")
    for question in args.questions:
        # Everything each named server offers. The runtime lists and loads the skills they serve itself.
        payload = {"text": question, "system_prompt": ASK_PROMPT, "model": args.model,
                   "servers": {name: {} for name in args.mcp}}
        say(f"\n> {question}")
        t0 = time.perf_counter()
        resp = client.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session_id, qualifier="DEFAULT",
                                           payload=json.dumps(payload).encode(), contentType="application/json",
                                           accept="text/event-stream")
        first = None
        # Small reads: iter_lines() reads 1 KB at a time by default, which holds back the first sentence.
        for raw in resp["response"].iter_lines(chunk_size=64):
            line = raw.decode() if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            kind = event.get("type")
            if kind == "text":
                first = first or time.perf_counter() - t0
                say(event["data"], end="")
            elif kind == "message_end":
                say("")
            elif kind == "tool":
                say(f"  [{event['kind']}] {event['text']}")
            elif kind == "done":
                say(f"  ({event['model']}, {event['seconds']}s in the runtime, first text after "
                    f"{first or 0:.1f}s, round trip {time.perf_counter() - t0:.1f}s, skills "
                    f"{[s['name'] if isinstance(s, dict) else s for s in event['skills_loaded']]})")
            else:  # {"type": "error"}, or the SDK's own {"error": ...} if the stream broke
                say(f"  ERROR {event.get('error')}")


def cmd_logs(args) -> None:
    aws = session(args)
    rt = find_runtime(aws.client("bedrock-agentcore-control"), args.name)
    if not rt:
        sys.exit(f"{args.name} isn't deployed")
    logs = aws.client("logs")
    prefix = f"/aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}"
    groups = [g["logGroupName"] for g in logs.describe_log_groups(logGroupNamePrefix=prefix).get("logGroups", [])]
    if not groups:
        sys.exit(f"No log group yet under {prefix} (it appears after the first invocation)")
    start = int((time.time() - args.minutes * 60) * 1000)
    for group in groups:
        say(f"== {group}")
        kwargs = {"logGroupName": group, "startTime": start}
        while True:
            page = logs.filter_log_events(**kwargs)
            for ev in page.get("events", []):
                say(f"{time.strftime('%H:%M:%S', time.localtime(ev['timestamp'] / 1000))} {ev['message'].rstrip()}")
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]


def cmd_delete(args) -> None:
    aws = session(args)
    ctl = aws.client("bedrock-agentcore-control")
    rt = find_runtime(ctl, args.name)
    if rt:
        say(f"Deleting runtime {args.name}")
        ctl.delete_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
        for _ in range(60):
            if not find_runtime(ctl, args.name):
                break
            time.sleep(5)
    iam = aws.client("iam")
    try:
        for name in iam.list_role_policies(RoleName=args.role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=args.role, PolicyName=name)
        iam.delete_role(RoleName=args.role)
        say(f"Deleted role {args.role}")
    except iam.exceptions.NoSuchEntityException:
        pass
    say("Done. The S3 bucket and its code zips stay; delete them yourself if you like.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", help="AWS profile that deploys (default: the usual credential chain)")
    p.add_argument("--invoke-profile", help="AWS profile `ask` invokes as (default: --profile)")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--name", default="gizmo_demo_brain", help="the AgentCore runtime's name")
    p.add_argument("--role", default="gizmo-demo-runtime", help="the runtime's IAM execution role")
    p.add_argument("--boundary", help="a permissions boundary policy name for the role, if your account uses one")
    p.add_argument("--bucket", help="S3 bucket for the code (default: gizmo-artifacts-<account>)")
    p.add_argument("--code-prefix", default="demo", help="S3 key prefix for the code zips")
    p.add_argument("--arn", help="the runtime's ARN, to skip looking it up (an invoke-only profile can't list runtimes)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("deploy", help="build, upload and create or update the runtime")
    s.add_argument("--env", action="append", metavar="KEY=VALUE", help="extra environment variables for the runtime")
    s.set_defaults(func=cmd_deploy)
    sub.add_parser("status", help="the runtime and its ARN").set_defaults(func=cmd_status)
    s = sub.add_parser("ask", help="invoke it the way the robot does, streaming the reply")
    s.add_argument("questions", nargs="+", help="one or more turns, in one session")
    s.add_argument("--session", help="a runtime session id (33+ characters) to keep using")
    s.add_argument("--model", choices=["haiku", "sonnet"], default="haiku")
    s.add_argument("--mcp", nargs="*", default=["explorer", "countries"], help="MCP servers the agent may use")
    s.set_defaults(func=cmd_ask)
    s = sub.add_parser("logs", help="recent runtime logs from CloudWatch")
    s.add_argument("--minutes", type=int, default=30)
    s.set_defaults(func=cmd_logs)
    sub.add_parser("delete", help="delete the runtime and its role (keeps the bucket)").set_defaults(func=cmd_delete)
    args = p.parse_args()
    try:
        args.func(args)
    except botocore.exceptions.ClientError as e:
        err = e.response["Error"]
        sys.exit(f"AWS {err['Code']}: {err['Message']}")


if __name__ == "__main__":
    main()
