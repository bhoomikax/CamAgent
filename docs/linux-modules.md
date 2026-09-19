# Linux Kernel Modules

## 1. Linux Kernel
The Linux kernel is the central core of the operating system. It manages CPU scheduling, memory, device drivers, filesystems, networking, and process coordination. In a running Linux system, the kernel is always in memory and stays active while the machine is up.

A kernel is not just a static binary. It contains a large set of code that handles devices and subsystems, and it can also load additional code at runtime through loadable kernel modules (LKMs).

## 2. User Space vs Kernel Space
User space is where normal programs run. Examples include Python scripts, shell commands, web servers, and desktop applications. These programs are restricted and do not directly manipulate hardware.

Kernel space is the privileged area where the operating system kernel executes. It has direct access to hardware and is trusted to manage system-critical resources.

The important boundary is:

- User space: unprivileged, application-facing
- Kernel space: privileged, hardware-facing

The kernel exposes safe interfaces to user space so programs can request services without violating system integrity.

## 3. Ring 0
Ring 0 is the highest privilege CPU execution level in x86-style architectures. Code running in Ring 0 has unrestricted access to memory, I/O ports, hardware registers, and kernel data structures.

The Linux kernel runs in Ring 0, which is why kernel code must be precise, validated, and carefully managed. If a non-kernel component were allowed to execute at this level, it could compromise the entire system.

This is the reason why operations such as module loading are tightly controlled and require root privileges.

## 4. Loadable Kernel Modules
A loadable kernel module is a small piece of kernel code that can be inserted into the running kernel without rebooting the system. Modules are commonly used for drivers, filesystems, network subsystems, and hardware support.

Examples include webcam drivers, USB device drivers, and filesystem handlers.

The benefits of LKMs include:

- modularity
- reduced kernel footprint
- easier driver management
- runtime extension of kernel functionality

The kernel can load a module when needed and unload it when no longer required, as long as the module is not in active use and permissions allow it.

## 5. lsmod
`lsmod` lists the modules currently loaded into the kernel.

Example:

```bash
lsmod
```

This command shows each loaded module, its size, and which modules depend on it. It is a quick way to inspect the live kernel state.

In a Python project, we can call `lsmod` and parse the output into a structured list so the rest of the system can reason about loaded modules in a predictable format.

## 6. modinfo
`modinfo` displays metadata about a kernel module, including information such as:

- module name
- filename
- description
- author
- version
- depends
- parameters

Example:

```bash
modinfo uvcvideo
```

This is useful when verifying whether a module exists, what it does, and whether it is a safe candidate for inspection or testing.

## 7. modprobe
`modprobe` loads a module and resolves its dependencies automatically.

Example:

```bash
sudo modprobe uvcvideo
```

This is preferable to manually managing a single module when dependent modules may also need to be loaded. The system handles dependency resolution, which makes `modprobe` the safest and most practical mechanism for normal module loading.

## 8. rmmod
`rmmod` removes a module directly from the running kernel.

Example:

```bash
sudo rmmod uvcvideo
```

This is a direct unload operation. It can work for a module that is safe to remove, but it does not always handle dependency logic as carefully as `modprobe -r`.

## 9. /proc/modules
`/proc/modules` is a Linux procfs interface that exposes the kernel's currently loaded modules.

Example:

```bash
cat /proc/modules | head
```

This file is a textual view of the live module list. Tools like `lsmod` usually read this information or a similar kernel state view and format it for human readability.

## 10. /sys/module
`/sys/module` is a sysfs interface that exposes kernel module state and metadata under the `/sys` hierarchy.

Example:

```bash
ls /sys/module | head
```

If a module is loaded, it usually has a corresponding directory under `/sys/module/<module>`. This gives a structured view that is valuable for checking whether a module is present and how the kernel exposes it.

## 11. Module dependencies
Kernel modules often depend on other modules. For example, a driver may require a common helper or a subsystem interface from another loaded module.

`modprobe` handles these dependencies so the required module stack is loaded in the correct order. This reduces errors and makes module management safer than manually trying to unload or load modules in an arbitrary sequence.

When reloading a module, the safe pattern is typically:

1. verify it is loaded
2. unload it
3. load it again
4. verify the state

This is the lifecycle our Python manager will model.

## 12. Permissions
Loading and unloading kernel modules is a privileged action. In most Linux environments, this requires root or sudo access.

Even when using a Python wrapper around the Linux commands, the effective permission boundary remains the same: the underlying system decides whether the operation is allowed.

This is important for security. A module manager should validate input and avoid constructing unsafe shell commands.

## 13. How our Python manager interacts with Linux
The project architecture is straightforward:

```text
Python
  ↓
module_manager.py
  ↓
Linux commands / interfaces
  ↓
Linux kernel module subsystem
```

The Python layer should not guess or invent behavior. Instead, it should use deterministic interfaces such as:

- `lsmod` for live module listing
- `modinfo` for module metadata
- `modprobe` for safe loading
- `modprobe -r` for safe unloading
- `/proc/modules` and `/sys/module` for verification

This creates a clean OS boundary where Python acts as a structured control layer between higher-level policy and the kernel itself.

---

