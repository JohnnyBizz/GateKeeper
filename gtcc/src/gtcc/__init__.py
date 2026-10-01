"""Grok Trading Command Center.

A multi-market trading research and execution platform.

Two rules hold everywhere in this package:

1. The deterministic risk engine in :mod:`gtcc.risk.engine` has final
   authority over every order. No language model, strategy or agent can
   reach a broker without passing it.
2. Nothing in here invents market data, fills, balances, news, backtest
   results or model confidence. When a datum is unavailable the code says
   so explicitly rather than substituting a plausible number.
"""

__version__ = "0.1.0"
