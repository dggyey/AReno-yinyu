Port conflicts
==============

When ``areno serve`` or ``areno proxy`` fails with ``Address already in use``,
another process is holding the target port.  AReno provides a built-in
diagnostic to identify the owner without terminating anything.

Diagnose the conflict
---------------------

.. code-block:: bash

   areno port-check --host 0.0.0.0 --port 8000

This prints the owning process PID, name, command line, and whether it appears
to be an AReno child process.  For scripting or dashboards:

.. code-block:: bash

   areno port-check --host 0.0.0.0 --port 8000 --json

Interpreting the result
-----------------------

* **Available** – the port is free; proceed with ``areno serve``.
* **Occupied by AReno child** – a previous AReno run may still be active.
  Stop it explicitly (e.g. ``kill <PID>``) or reuse the running instance.
* **Occupied by unrelated service** – change the ``--port`` flag on
  ``areno serve`` or stop the conflicting process manually.
* **Owner unknown** – the diagnostic could not inspect the owning process
  (common on Linux when the process belongs to another user).  The original
  bind error is preserved.  Run with elevated privileges or choose a different
  port.

Automatic diagnostic at startup
-------------------------------

``areno serve`` runs the port diagnostic automatically before model
initialization.  When the port is held by an unrelated process, the command
exits immediately with the diagnostic report, avoiding a wasted model load.
