from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from test_f5o2_storage import pg_server, persisted


async def test_migration_preserves_historical_ownerless_requests(persisted):
    db, _, _ = persisted
    async with db.transaction() as session:
        assert await session.scalar(text("select to_regclass('tb_billing_attempt')::text")) == 'tb_billing_attempt'
        rid = uuid4()
        await session.execute(text("""insert into tb_llm_request(request_id,operation,status,created_at)
            values (:id,'completion','failed',now())"""), {'id': rid})
        assert await session.scalar(text('select agent_id from tb_llm_request where request_id=:id'), {'id': rid}) is None


from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from contracts import Usage
from errors import GatewayError
from persistence import billing
from services.prices import PriceQuote


def paid_quote(amount='0.008'):
    return PriceQuote('supplier-main', 'flash', 'v1', 'CNY', 'fixed', '0.4', '2', '8',
                      250, 100, Decimal(amount), None)


async def owned_attempt(session, owner, number=1):
    rid = uuid4()
    pk = await session.scalar(text("""insert into tb_llm_request(agent_id,request_id,operation,status,created_at)
        values (:owner,:id,'completion','running',now()) returning id"""), {'owner':owner, 'id':rid})
    await session.execute(text("""insert into tb_llm_attempt(request_pk,attempt_no,model,provider,adapter,
        api_protocol,configured_model,status,started_at)
        values (:pk,:number,'primary','wire','deepseek','chat_completions','flash','running',now())"""),
        {'pk': pk, 'number':number})
    return {'id':pk, 'agent_id':owner,'request_id':rid}, SimpleNamespace(
        number=number, adapter='deepseek', actual_model='flash', configured_model='flash',
        billing_observed_at=datetime.now(timezone.utc),
        usage=Usage(input_tokens=200,output_tokens=20,total_tokens=220),cached_input_tokens=150,provider_entered=True)


async def account(session, amount='1.000'):
    await session.execute(text("insert into tb_agent(agent_id,name) values ('ledger-a','A')"))
    return await billing.deposit(session,'ledger-a','init',Decimal(amount))


async def test_known_cost_deposit_replay_and_conflicting_operation(persisted):
    db, _, _ = persisted
    async with db.transaction() as s:
        assert (await account(s))['available'] == 1
        assert (await billing.deposit(s,'ledger-a','init','1'))['available'] == 1
        row, rec = await owned_attempt(s, 'ledger-a')
        await billing.reserve(s,row,rec,paid_quote('0.001300'))
        assert (await billing.balance(s,'ledger-a'))['available'] == Decimal('.998700')
        await billing.settle(s,row,rec)
        await billing.settle(s,row,rec)
        assert (await billing.balance(s,'ledger-a'))['available'] == Decimal('.999680')
    with pytest.raises(GatewayError,match='操作编号'):
        async with db.transaction() as s:
            await billing.deposit(s,'ledger-a','init','2')


@pytest.mark.parametrize('action,amount,state,charge,supplier',[
    ('settle','.008','settled',Decimal('.008'),Decimal('.008')),
    ('zero',None,'released',0,0), ('platform',None,'platform',0,None)])
async def test_recovered_hold_manual_resolution_once(persisted,action,amount,state,charge,supplier):
    db,_,_ = persisted
    async with db.transaction() as s:
        await account(s)
        row,rec = await owned_attempt(s,'ledger-a')
        await billing.reserve(s,row,rec,paid_quote())
        await billing.recover_billing(s,row)
        assert (await billing.balance(s,'ledger-a'))['pending_attempts'] == 1
        for _ in range(2):
            await billing.resolve(s,row['request_id'],1,'resolve',action,amount,'statement evidence')
        record=(await s.execute(text('select state,agent_charge,supplier_cost from tb_billing_attempt'))).one()
        assert tuple(record)==(state,charge,supplier)
        assert (await billing.balance(s,'ledger-a'))['reserved']==0


@pytest.mark.parametrize('actual,cache', [('different',150),('flash',None)])
async def test_uncertain_evidence_bills_nothing_until_known(persisted,actual,cache):
    db,_,_ = persisted
    async with db.transaction() as s:
        await account(s)
        row,rec = await owned_attempt(s,'ledger-a')
        rec.actual_model,rec.cached_input_tokens=actual,cache
        await billing.reserve(s,row,rec,paid_quote())
        await billing.settle(s,row,rec)
        assert (await billing.balance(s,'ledger-a'))['pending']==Decimal('.008')


async def test_statement_compares_known_and_unknown_without_allocating(persisted):
    db,_,_=persisted
    async with db.transaction() as s:
        await account(s)
        for known in (True,False):
            row,rec = await owned_attempt(s,'ledger-a')
            await billing.reserve(s,row,rec,paid_quote())
            if not known: rec.usage=None
            await billing.settle(s,row,rec)
        now=datetime.now(timezone.utc)
        args=('stmt','supplier-main','CNY',now-timedelta(hours=1),now+timedelta(hours=1),'0.001','supplier report')
        for inserted in (True,False):
            report = await billing.reconcile_statement(s,*args)
            assert report['newly_recorded'] is inserted
            assert report['subtotal']==Decimal('0.000320') and report['unknown_attempts']==1
            assert report['unassigned_difference']==Decimal('0.000680')
        assert (await billing.balance(s,'ledger-a'))['pending']==Decimal('.008')


async def test_concurrent_reservations_serialize_on_agent(persisted):
    import asyncio
    db,_,_=persisted
    async with db.transaction() as s:
        await account(s,'.010')
        attempts=[await owned_attempt(s,'ledger-a') for _ in range(2)]
    async def run(item):
        try:
            async with db.transaction() as s:
                await billing.reserve(s,*item,paid_quote())
            return 'ok'
        except GatewayError as exc:
            return exc.code
    assert sorted(await asyncio.gather(*(run(item) for item in attempts)))==['insufficient_budget','ok']
    async with db.transaction() as s:
        assert (await billing.balance(s,'ledger-a'))['available']==Decimal('.002')


async def test_supplier_evidence_after_platform_release_does_not_charge_agent(persisted):
    db,_,_=persisted
    async with db.transaction() as s:
        await account(s)
        row,rec=await owned_attempt(s,'ledger-a')
        await billing.reserve(s,row,rec,paid_quote())
        await billing.recover_billing(s,row)
        await billing.resolve(s,row['request_id'],1,'waive','platform',None,'platform assumes fee')
        for _ in range(2):
            await billing.resolve(s,row['request_id'],1,'invoice','supplier','.002','verified invoice')
        record = (await s.execute(text('select state,agent_charge,supplier_cost from tb_billing_attempt'))).one()
        assert tuple(record)==('platform',0,Decimal('.002'))
        assert (await billing.balance(s,'ledger-a'))['available']==1


async def test_commit_ack_loss_then_retry_never_reapplies_charge(persisted,monkeypatch):
    from persistence.database import StorageUnavailable
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    db,_,_=persisted
    async with db.transaction() as s:
        await account(s)
        row,rec=await owned_attempt(s,'ledger-a')
        await billing.reserve(s,row,rec,paid_quote())
    original=AsyncSessionTransaction.__aexit__
    async def lose_ack(transaction,*args):
        await original(transaction,*args)
        raise OSError('test: lost commit acknowledgement')
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSessionTransaction,'__aexit__',lose_ack)
        with pytest.raises(StorageUnavailable):
            async with db.transaction() as s:
                await billing.settle(s,row,rec)
    async with db.transaction() as s:
        await billing.recover_billing(s,row)
        await billing.settle(s,row,rec)
        assert (await billing.balance(s,'ledger-a'))['available']==Decimal('.999680')


async def test_competing_manual_operations_only_one_commits(persisted):
    import asyncio
    db,_,_=persisted
    async with db.transaction() as s:
        await account(s)
        row,rec=await owned_attempt(s,'ledger-a')
        await billing.reserve(s,row,rec,paid_quote())
        await billing.recover_billing(s,row)
    async def resolve(idx):
        try:
            async with db.transaction() as s:
                await billing.resolve(s,row['request_id'],1,str(idx),'settle','.003','verified receipt')
            return 'ok'
        except GatewayError as err:
            return err.code
    results=await asyncio.gather(resolve(1),resolve(2))
    assert sorted(results)==['billing_state_conflict','ok']
    async with db.transaction() as s:
        assert (await billing.balance(s,'ledger-a'))['available']==Decimal('.997')
        assert await s.scalar(text("select count(*) from tb_billing_operation where kind='settle'"))==1


async def test_budget_can_be_negative_after_actual_settlement(persisted):
    db,_,_=persisted
    async with db.transaction() as s:
        await account(s,'.000100')
        row,rec=await owned_attempt(s,'ledger-a')
        await billing.reserve(s,row,rec,paid_quote('.000100'))
        await billing.settle(s,row,rec)
        assert (await billing.balance(s,'ledger-a'))['available']==Decimal('-.000220')
    with pytest.raises(GatewayError,match='预算不足'):
        async with db.transaction() as s:
            row,rec=await owned_attempt(s,'ledger-a')
            await billing.reserve(s,row,rec,paid_quote())
    async with db.transaction() as s:
        assert (await billing.deposit(s,'ledger-a','credit-2','1.0'))['available']==Decimal('.999780')


async def test_amounts_exact_at_scale_boundaries(persisted):
    db,_,_=persisted
    async with db.transaction() as s:
        await account(s,'100000000000000000000')
        await billing.deposit(s,'ledger-a','tiny','0.000000000000000000000001')
        assert (await billing.balance(s,'ledger-a'))['available']==Decimal('100000000000000000000.000000000000000000000001')
    for invalid in ('NaN','Infinity','-1','1e-25','1e21','0'):
        with pytest.raises(GatewayError):
            async with db.transaction() as s:
                await billing.deposit(s,'ledger-a','invalid',invalid)


@pytest.mark.parametrize('observation,expected_state', [
    ('2025-12-31T23:59:59.999999+00:00','settled'),
    ('2026-01-01T00:00:00+00:00','pending'),
    (None,'pending'),
])
async def test_paid_settlement_uses_terminal_observation_not_write_time(persisted, observation, expected_state):
    from dataclasses import replace
    db,_,_=persisted
    # The terminal report and the price switch are in the past when PG commits.
    boundary = '2026-01-01T00:00:00+00:00'
    quote = replace(paid_quote(), hit='2', miss='2', valid_until=boundary if observation else None)
    async with db.transaction() as s:
        await account(s)
        row,rec = await owned_attempt(s,'ledger-a')
        rec.billing_observed_at = datetime.fromisoformat(observation) if observation else None
        await billing.reserve(s,row,rec,quote)
        await billing.settle(s,row,rec)
        state,charge = (await s.execute(text('select state,agent_charge from tb_billing_attempt'))).one()
        assert state == expected_state
        assert charge == (Decimal('.000560') if expected_state=='settled' else None)
        assert (await s.scalar(text('select billing_observed_at from tb_billing_attempt'))) == rec.billing_observed_at


async def test_paid_attempt_waiting_for_account_lock_retains_pre_boundary_evidence(persisted):
    import asyncio
    from dataclasses import replace
    db,_,_=persisted
    boundary = datetime.now(timezone.utc) + timedelta(milliseconds=400)
    async with db.transaction() as s:
        await account(s)
        row,rec = await owned_attempt(s,'ledger-a')
        await billing.reserve(s,row,rec,replace(paid_quote(),valid_until=boundary.isoformat()))
    async def save():
        async with db.transaction() as s:
            await billing.settle(s,row,rec)
    async with db.transaction() as blocker:
        await blocker.execute(text("select agent_id from tb_agent where agent_id='ledger-a' for update"))
        rec.billing_observed_at=datetime.now(timezone.utc)
        assert rec.billing_observed_at < boundary
        task=asyncio.create_task(save())
        await asyncio.sleep(max(0,(boundary-datetime.now(timezone.utc)).total_seconds())+.02)
        assert not task.done()
    await task
    async with db.transaction() as s:
        state,stamp=(await s.execute(text('select state,billing_observed_at from tb_billing_attempt'))).one()
        assert state=='settled' and stamp==rec.billing_observed_at
