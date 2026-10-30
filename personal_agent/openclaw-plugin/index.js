import { readFileSync } from 'node:fs';
const tools = JSON.parse(readFileSync(new URL('./tools.json', import.meta.url), 'utf8'));
export default {
  id: 'haixin-tools', name: '海辛受控工具',
  register(api) {
    for (const tool of tools) api.registerTool({
      ...tool, label: tool.name, executionMode: 'sequential',
      async execute(id, args, signal) {
        const response = await fetch(process.env.HAIXIN_TOOL_URL, {
          method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.HAIXIN_TOOL_TOKEN}` },
          body: JSON.stringify({ id, name: tool.name, args }),
          signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(45000)]) : AbortSignal.timeout(45000),
        });
        const details = await response.json();
        return { content: [{ type: 'text', text: JSON.stringify(details) }], details, isError: !details.ok };
      },
    });
  },
};
