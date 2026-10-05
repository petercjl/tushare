"""Credential-safe stdio bridge to the user-configured remote data MCP."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
from .runtime import mcp_url, safe_error

# Remote URLs include credentials; avoid HTTP request logging.
logging.getLogger('httpx').setLevel(logging.CRITICAL)
logging.getLogger('httpcore').setLevel(logging.CRITICAL)
logging.getLogger('httpx2').setLevel(logging.CRITICAL)
logging.getLogger('httpcore2').setLevel(logging.CRITICAL)
logging.getLogger('mcp.client.streamable_http').setLevel(logging.CRITICAL)

@asynccontextmanager
async def upstream():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client, create_mcp_http_client
    import httpx2
    async with create_mcp_http_client(timeout=httpx2.Timeout(20, connect=10)) as client, streamable_http_client(mcp_url(), http_client=client) as streams:
        async with ClientSession(streams[0], streams[1], read_timeout_seconds=20) as session:
            await session.initialize()
            yield session

async def inspect_remote():
    async with upstream() as session:
        result = await session.list_tools()
        return {'connected': True, 'tools': [tool.model_dump(mode='json') for tool in result.tools]}

def clean(value):
    return json.loads(safe_error_long(json.dumps(value, ensure_ascii=False, default=str)))

def safe_error_long(text):
    from .runtime import token
    return text.replace(token(), '[REDACTED]')

async def run_server():
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    async def list_tools(context, params):
        return types.ListToolsResult(tools=[
            types.Tool(name='list_remote_data_tools', description='Discover available read-only Tushare data tools; listing is not permission verification',
                       inputSchema={'type':'object','properties':{},'additionalProperties':False}),
            types.Tool(name='call_remote_data_tool', description='Call a configured remote financial data tool; permissions still apply',
                       inputSchema={'type':'object','properties':{'tool_name':{'type':'string'},'arguments':{'type':'object'}},
                                    'required':['tool_name','arguments'],'additionalProperties':False})])
    async def call_tool(context, params):
        try:
            if params.name == 'list_remote_data_tools':
                output = await inspect_remote()
                output['tools'] = [t for t in output['tools'] if t['name'] not in ('p_save', 'p_delete')]
            elif params.name == 'call_remote_data_tool':
                args = params.arguments or {}
                name, arguments = args['tool_name'], args['arguments']
                if name in ('p_save', 'p_delete') or any(k in arguments for k in ('token','url','endpoint','ts_type_name')):
                    raise ValueError('Only read-only data with managed authentication is supported')
                async with upstream() as session:
                    names = {t.name for t in (await session.list_tools()).tools}
                    if name not in names: raise ValueError('Unknown remote data tool')
                    result = await session.call_tool(name, arguments)
                    return types.CallToolResult.model_validate(clean(result.model_dump(mode='json')))
            else: raise ValueError('Unknown bridge tool')
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(clean(output),ensure_ascii=False))])
        except Exception as exc:
            return types.CallToolResult(isError=True,content=[types.TextContent(type='text',text=safe_error(exc))])
    server = Server('tushare-data-bridge', version='0.1.0', on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as streams:
        await server.run(streams[0],streams[1],server.create_initialization_options())

def serve():
    asyncio.run(run_server())
    return 0

def main(action):
    if action == 'serve': return serve()
    try:
        result = asyncio.run(inspect_remote())
        print(json.dumps({'ok':True,'result':{'connected':True,'tool_count':len(result['tools']),
              'core_tools': [t['name'] for t in result['tools'] if t['name'] in ('trade_cal','fund_daily','fund_nav','fund_adj','etf_mins','stk_mins','rt_etf_k')]}},ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'error':{'code':type(exc).__name__,'message':safe_error(exc)}},ensure_ascii=False))
        return 1

