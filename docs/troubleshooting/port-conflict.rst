Port conflicts
==============

When ``areno serve`` or ``areno proxy`` fails with ``Address already in use``,
another process is holding the target port.  AReno provides a built-in
diagnostic to identify the owner without terminating anything.

Minimal runnable example
------------------------

The following example can be run on any machine without a GPU, model weights,
or AReno engine dependencies.  It checks a port that is almost certainly free
(59999) and then a port that is deliberately occupied:

.. code-block:: bash

   # 1. Check a free port – exit code 0
   areno port-check --host 127.0.0.1 --port 59999

   # 2. Occupy port 59998, then check it – exit code 1
   python -c "import socket, time; s=socket.socket(); s.bind(('127.0.0.1',59998)); s.listen(1); time.sleep(30)" &
   sleep 1
   areno port-check --host 127.0.0.1 --port 59998

   # 3. Machine-readable JSON output
   areno port-check --host 127.0.0.1 --port 59999 --json

Observable output
~~~~~~~~~~~~~~~~~

When the port is **available** (step 1), the terminal shows:

.. code-block:: text

   Port diagnostic: 127.0.0.1:59999
     Status: available
     Suggestion: Port is available for binding.

When the port is **occupied** (step 2), the terminal shows:

.. code-block:: text

   Port diagnostic: 127.0.0.1:59998
     Status: occupied
     Bind error: [Errno 98] Address already in use
     Suggestion: Port 59998 is occupied but the owning process could not be
       identified (possible permission restriction). Original error: ...

JSON output (step 3) returns all fields:

.. code-block:: json

   {
     "host": "127.0.0.1",
     "port": 59999,
     "available": true,
     "pid": null,
     "process_name": null,
     "cmdline": null,
     "is_areno_child": false,
     "bind_error": null,
     "suggestion": "Port is available for binding."
   }

Exit codes: ``0`` when the port is available, ``1`` when it is occupied.

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
