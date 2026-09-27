# Building signal designer

The /building-signal-design?project=<owner>:<number> page is the separate
building-signal surface. It generates one bounded inventory comparison through
the local structured-output model adapter, lets an operator correct the JSON,
and persists the explicit draft -> confirmed -> assigned lifecycle.

The page uses the scoped building-signal API and never stores credentials in the
browser. Reload readback reads the persisted rule and current signal state;
periodic evaluation runs through the existing scheduler cadence rather than a
second timer.

The first supported rule shape is:

    {
      "schema_version": 1,
      "comparison": "lte",
      "quantity": 5,
      "item": "iron-plate",
      "signal": "green"
    }
