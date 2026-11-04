"""Local administrator commands. Run with PYTHONPATH=src python -m admin."""
import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import json
import sys
from uuid import UUID

from api.identity import create_agent, issue_credential, revoke_credential, rotate_credential
from errors import GatewayError
from persistence import billing
from persistence.database import Database, load_database_config
from services.models import DEFAULT_CONFIG_DIR


def command_line():
    parser = argparse.ArgumentParser(description='LLM Gateway Agent 与账务管理')
    parser.add_argument('--config-dir', type=Path, default=DEFAULT_CONFIG_DIR)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('create-agent')
    create.add_argument('agent_id')
    create.add_argument('name')
    for name in ('issue', 'rotate', 'revoke'):
        p = commands.add_parser(name)
        p.add_argument('agent_id')
        if name != 'issue':
            p.add_argument('public_id')
    p = commands.add_parser('deposit')
    p.add_argument('agent_id')
    p.add_argument('amount')
    p.add_argument('--operation-id', required=True)
    p = commands.add_parser('resolve')
    p.add_argument('request_id', type=UUID)
    p.add_argument('attempt_no', type=int)
    p.add_argument('decision', choices=['settle','zero','platform','supplier'])
    p.add_argument('--amount')
    p.add_argument('--reason', required=True)
    p.add_argument('--operation-id', required=True)
    p = commands.add_parser('statement')
    p.add_argument('supplier')
    p.add_argument('start', type=datetime.fromisoformat)
    p.add_argument('end', type=datetime.fromisoformat)
    p.add_argument('amount')
    p.add_argument('--operation-id', required=True)
    p.add_argument('--reason', required=True)
    return parser


async def run(args):
    database = Database(load_database_config(args.config_dir), args.config_dir)
    try:
        await database.check()
        async with database.transaction() as session:
            if args.action == 'create-agent':
                await create_agent(session, args.agent_id, args.name)
            elif args.action == 'issue':
                return await issue_credential(session, args.agent_id)
            elif args.action == 'rotate':
                return await rotate_credential(session, args.agent_id, args.public_id)
            elif args.action == 'revoke':
                await revoke_credential(session, args.agent_id, args.public_id)
            elif args.action == 'deposit':
                return await billing.deposit(session, args.agent_id, args.operation_id, args.amount)
            elif args.action == 'resolve':
                return await billing.resolve(session, args.request_id, args.attempt_no, args.operation_id,
                                             args.decision, args.amount, args.reason)
            elif args.action == 'statement':
                return await billing.reconcile_statement(session, args.operation_id, args.supplier,
                    'CNY', args.start, args.end, args.amount, args.reason)
    finally:
        await database.close()


def main():
    args = command_line().parse_args()
    try:
        result = asyncio.run(run(args))
    except GatewayError as exc:
        print(f'{exc.code} {exc.message}', file=sys.stderr)
        return 1
    except ValueError:
        print('命令参数或配置无效', file=sys.stderr)
        return 1
    if args.action in {'issue', 'rotate'}:
        print(result)
    else:
        print(json.dumps({'success':True,'data':result}, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
