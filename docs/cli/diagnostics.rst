:orphan:

Diagnostics CLI reference
=========================

``areno env`` and ``areno check`` help diagnose setup problems before a user
hits low-level Python, CUDA, or PyTorch errors.

``areno env`` is a descriptive support report. It does not initialize the AReno
engine or load model weights. Use it when collecting information for an issue.

.. code-block:: bash

   areno env

For machine-readable issue reports:

.. code-block:: bash

   areno env --json

The report includes:

* AReno version
* Python version and executable
* OS, platform, and architecture
* PyTorch version, CUDA build, CUDA runtime, and CUDA availability
* CUDA driver information from ``nvidia-smi`` when available
* visible GPU count, names, and compute capability
* ``CUDA_HOME`` and inferred CUDA toolkit location
* ``nvcc`` path and version
* ``flash-attn`` import status and version
* ``flash-linear-attention`` import status and version
* ``areno_accel`` import status
* selected environment variables such as ``MAX_JOBS``,
  ``CUDA_VISIBLE_DEVICES``, and ``TORCH_CUDA_ARCH_LIST``

areno check
-----------

``areno check`` validates whether the machine is ready to run AReno training
and serving. It classifies each check as ``OK``, ``WARN``, or ``FAIL`` and
prints concrete next steps for failures.

.. code-block:: bash

   areno check

Example output:

.. code-block:: text

   AReno check: not ready

   OK   Python >= 3.10
        found 3.11.8
   OK   PyTorch CUDA build
        torch.version.cuda=12.4
   OK   CUDA_HOME
        not set (not required for runtime; areno_accel imports)

``CUDA_HOME`` and ``nvcc`` are only warnings when AReno needs to build its CUDA
extension. If the installed ``areno_accel`` extension imports successfully,
they are not required for runtime readiness.

Checks include:

* Python version
* supported platform
* PyTorch import and version
* PyTorch CUDA build
* ``torch.cuda.is_available()``
* NVIDIA GPU visibility
* ``CUDA_HOME`` and ``nvcc``
* optional runtime dependency imports
* ``areno_accel`` import
* writable cache/log locations

``WARN`` items usually indicate degraded or incomplete setup. ``FAIL`` items
mean AReno is not ready to run the CUDA training/inference engine.

areno port-check
----------------

``areno port-check`` diagnoses whether a target ``(host, port)`` is available
for ``areno serve`` or ``areno proxy`` startup.  If the port is occupied, the
command identifies the owning process, classifies it as an AReno child process
or an unrelated external service, and prints a safe, actionable suggestion.
The diagnostic **never terminates any process**.

.. code-block:: bash

   areno port-check --host 0.0.0.0 --port 8000

For machine-readable output (e.g. scripting):

.. code-block:: bash

   areno port-check --host 0.0.0.0 --port 8000 --json

Example output (port occupied by an unrelated service):

.. code-block:: text

   Port diagnostic: 0.0.0.0:8000
     Status: occupied
     PID: 4321
     Process: nginx
     AReno child: no
     Command: nginx -g daemon off
     Bind error: [Errno 98] Address already in use
     Suggestion: Port 8000 is held by 'nginx' (PID 4321). Change the port or stop that process manually.

The diagnostic also runs automatically before ``areno serve`` startup, before
expensive model or worker initialization.  When the port is held by an unrelated
process, ``areno serve`` exits with an error.  When held by an AReno child
process, it prints a warning and continues (the subsequent bind may still fail).

Output fields (JSON mode):

* ``host`` – bind host
* ``port`` – port number
* ``available`` – ``true`` if the port is free for binding
* ``pid`` – PID of the process holding the port (``null`` if unknown)
* ``process_name`` – name of the owning process
* ``cmdline`` – command line of the owning process
* ``is_areno_child`` – ``true`` if the process appears to belong to AReno
* ``bind_error`` – original OS error message if bind failed
* ``suggestion`` – recommended next action

Limitations:

* Process inspection requires sufficient OS permissions.  On Linux, non-root
  users may not see processes owned by other users.  When the owner cannot be
  determined, the original bind error is preserved.
* Bind races: the port may become occupied between the diagnostic check and the
  actual startup bind.  In this case the original bind error from the server is
  surfaced.
* Supports both IPv4 and IPv6 address families.
