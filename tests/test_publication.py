import asyncio
import os

import httpx
import pytest

from ai_builder.evidence import ArtifactStore
from ai_builder.pg_execution import execution_pool
from ai_builder.pg_migrate import migrate
from ai_builder.publish_run import publish_existing
from ai_builder.research_scope import run_research
from test_research_cli import context, ResearchProviders, sources, fixture_parser

DSN = os.getenv('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='Requires disposable PostgreSQL')


@pytest.mark.parametrize('purpose', ['eval', 'live'])
def test_saved_result_publication_is_read_only_and_preserves_scenario(config, tmp_path, purpose):
    async def check():
        await migrate(DSN)
        async with execution_pool(DSN) as pool:
            inputs = context().model_copy(update={'purpose':purpose})
            fixture = ResearchProviders(pool, inputs)
            fetcher, _, _ = sources()
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                result = await run_research(pool, ArtifactStore(tmp_path/'evidence'),
                    config=config, context=inputs, study_budget='25', openai_key='fixture',
                    tavily_key='fixture', provider_client=client, fetch_document=fetcher.fetch,
                    parser=fixture_parser)
            calls = len(fixture.calls)
            metadata = dict(id='fixture',title='Fixture',summary='Synthetic test only',
                            buyer='Fixture buyers',revenue='Subscription')
            if purpose == 'eval':
                with pytest.raises(ValueError, match='completed live'):
                    await publish_existing(pool, tmp_path, result.run_id, metadata)
                assert not (tmp_path/'public').exists()
            else:
                from ai_builder.public import load_snapshot
                await publish_existing(pool, tmp_path, result.run_id, metadata)
                saved = load_snapshot(tmp_path)
                assert saved['status'] == 'partial' and saved['selected_count'] == 1
                assert saved['scenario']['jurisdiction'] == config.scenario.jurisdiction
                assert len(saved['models'][0]['sources']) == 2
                assert 'documents' not in saved['models'][0]
                assert 'OPENAI_API_KEY' not in str(saved)
            assert len(fixture.calls) == calls
    asyncio.run(check())
