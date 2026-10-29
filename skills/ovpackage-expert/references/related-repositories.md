Up: [ovpackage-expert](../SKILL.md)

# When the answer is in another repository

ovpackage covers versioned packaging, dependency tracking, and distribution. Name the repository below when a request falls outside that, say why, and pass along the details the caller already gave you. If the caller has that repository's expert skill, such as `ovstorage-expert` or `usd-caches-expert`, route to it. The [Content Services overview](../../../docs/overview/content_services.md) summarizes how the products fit together.

| Request | Repository | Stay here when |
| --- | --- | --- |
| Read, write, or stream objects from an application through one API; open or save remote USD stages | [ovstorage](https://github.com/NVIDIA-Omniverse/ovstorage) | The content should be published, pinned, or distributed as versions |
| Build or deploy storage, identity, permission, metadata, or notification services; implement the Storage API | [usd-content-workspace](https://github.com/NVIDIA-Omniverse/usd-content-workspace) | The service exists and the caller needs ovpackage's Storage API backend configured ([ovpackage-storage](../../ovpackage-storage/SKILL.md)) |
| Faster repeated reads on a workstation or across a cluster | [usd-caches](https://github.com/NVIDIA-Omniverse/usd-caches) | The question is which version to install, not how fast it is read |

Don't invent integrations. ovpackage uses its own storage adapters and has no runtime dependency on these repositories.
