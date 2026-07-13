# JianYing protocol templates

These JSON protocol templates are pinned to
[`duoec/duo-video`](https://github.com/duoec/duo-video) commit
`ef4eb46c823910553f901649f2f13fd7575e748f`, under its MIT license. They are
data/schema baselines, not executable upstream code. Runtime builders deep-copy
the templates and replace authored values such as IDs, paths, timings, canvas
dimensions, and resource configuration.

The required copyright and permission notice is preserved in
`LICENSE.duo-video` in this directory.

The exporter never uses duo-video's embedded example credentials. Resource-ID
features accept an offline `material` or `resource_config` payload so official
resource packages can be supplied legally by the caller.
