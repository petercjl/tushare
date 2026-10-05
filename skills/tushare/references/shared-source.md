# Shared local / PTrade source
Input is one complete strategy and tested local/broker service contracts. Default global RUNTIME_MODE=ptrade; the local loader reads STRATEGY_RUNTIME=local before executing the same source and injects _LOCAL_API. Core calculations/decisions are identical, provider data/account/orders are adapted. Missing services fail explicitly without switching platforms silently.

Use `backtest seal-strategy --file SOURCE --out NEW_FILE` after edits; it seals AST identity excluding only formatting/comments and top-level token/identity values. Local loader verifies identity; PTrade logs declare it. Actual PTrade cached source must still be read and compared: a declared ID alone is not proof of server source. Preserve raw SHA alongside build ID.

S02 is the implemented shared-source runner. For another strategy define lifecycle/data/order/account services, test identical fixed inputs and meaningful signal/fill cases, implement a discoverable runner, then broker-test the actual source. Do not promise identical profits: data visibility, adjustment, limits, cash freezing and fills may differ. Broker SDK tokens and public Tushare tokens are distinct; keep private values out of public examples. Current S02 keeps the backtest-only guard.

Output complete sealed source, actual mode/services, version/build identity and unresolved contracts; return to select/write or broker handoff.
Source: U shared-source requirement; E explicit dispatcher and replay regression.
