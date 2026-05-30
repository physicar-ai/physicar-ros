#!/usr/bin/env python3
"""
Agent Node - Tool management services

Services:
- /agent/tool/list   : List tools
- /agent/tool/get    : Get tool details / source code
- /agent/tool/call   : Run a tool
- /agent/tool/set    : Save entire tools.py and reload
- /agent/tool/reload : Reload tools.py from disk
- /agent/tool/reset  : Reset (restore builtins)

System tools (reserved, cannot be used as function names):
- tool_list   : List tools
- tool_get    : Get tool details / source code
- tool_set    : Save entire tools.py
- tool_reload : Reload tools from disk
- tool_reset  : Reset
"""

import json
import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup

from physicar_interfaces.srv import (
    ToolList,
    ToolGet,
    ToolCall,
    ToolSet,
    ToolReload,
    ToolReset,
)

from physicar_agent import registry


class AgentNode(Node):
    def __init__(self):
        super().__init__('agent_node')

        tool_count = registry.init_tools()
        self.get_logger().info(f"Loaded {tool_count} tools into cache")

        self._srv_cb_group = ReentrantCallbackGroup()

        self.list_srv = self.create_service(
            ToolList, '/agent/tool/list', self.handle_list,
            callback_group=self._srv_cb_group
        )
        self.get_srv = self.create_service(
            ToolGet, '/agent/tool/get', self.handle_get,
            callback_group=self._srv_cb_group
        )
        self.call_srv = self.create_service(
            ToolCall, '/agent/tool/call', self.handle_call,
            callback_group=self._srv_cb_group
        )
        self.set_srv = self.create_service(
            ToolSet, '/agent/tool/set', self.handle_set,
            callback_group=self._srv_cb_group
        )
        self.reload_srv = self.create_service(
            ToolReload, '/agent/tool/reload', self.handle_reload,
            callback_group=self._srv_cb_group
        )
        self.reset_srv = self.create_service(
            ToolReset, '/agent/tool/reset', self.handle_reset,
            callback_group=self._srv_cb_group
        )

        self.get_logger().info("Agent services ready")

    def handle_list(self, request, response):
        try:
            tools = registry.list_tools(include_system=request.include_system)
            response.tools_json = json.dumps(tools, ensure_ascii=False)
        except Exception as e:
            self.get_logger().error(f"List error: {e}")
            response.tools_json = json.dumps([])
        return response

    def handle_get(self, request, response):
        try:
            info = registry.get_tool_info(request.name)
            if info is None:
                response.found = False
                response.info_json = json.dumps({"error": f"Tool '{request.name}' not found"})
            else:
                response.found = True
                if not request.include_code and 'code' in info:
                    info = info.copy()
                    del info['code']
                response.info_json = json.dumps(info, ensure_ascii=False)
        except Exception as e:
            self.get_logger().error(f"Get error: {e}")
            response.found = False
            response.info_json = json.dumps({"error": str(e)})
        return response

    def _call_system_tool(self, name: str, args: dict) -> list:
        if name == "tool_list":
            include_system = args.get("include_system", False)
            tools = registry.list_tools(include_system=include_system)
            return [{"type": "text", "text": json.dumps(tools, ensure_ascii=False)}]

        elif name == "tool_get":
            tool_name = args.get("name")
            if not tool_name:
                return [{"type": "text", "text": "Error: 'name' is required"}]
            info = registry.get_tool_info(tool_name)
            if info is None:
                return [{"type": "text", "text": json.dumps({"error": f"Tool '{tool_name}' not found"}, ensure_ascii=False)}]
            return [{"type": "text", "text": json.dumps(info, ensure_ascii=False)}]

        elif name == "tool_set":
            code = args.get("code")
            if not code:
                return [{"type": "text", "text": "Error: 'code' is required"}]
            result = registry.set_tools(code)
            return [{"type": "text", "text": json.dumps({"success": result.success, "message": result.message, "tool_count": result.tool_count}, ensure_ascii=False)}]

        elif name == "tool_reload":
            count = registry.reload_tools()
            return [{"type": "text", "text": json.dumps({"success": True, "tool_count": count}, ensure_ascii=False)}]

        elif name == "tool_reset":
            count = registry.reset_tools()
            return [{"type": "text", "text": json.dumps({"success": True, "tool_count": count}, ensure_ascii=False)}]

        else:
            return [{"type": "text", "text": f"Unknown system tool: {name}"}]

    def handle_call(self, request, response):
        try:
            args = json.loads(request.args_json) if request.args_json else {}

            if registry.is_system_tool(request.name):
                result = self._call_system_tool(request.name, args)
                response.success = True
                response.result_json = json.dumps(result, ensure_ascii=False)
            else:
                result = registry.call_tool(request.name, args)
                response.success = True
                response.result_json = json.dumps(result, ensure_ascii=False)
        except Exception as e:
            import traceback
            self.get_logger().error(f"Call error: {e}")
            response.success = False
            response.result_json = json.dumps([{"type": "text", "text": f"Error: {e}\n{traceback.format_exc()}"}])
        return response

    def handle_set(self, request, response):
        try:
            result = registry.set_tools(request.code)
            response.success = result.success
            response.message = result.message
            response.tool_count = result.tool_count
            if result.success:
                self.get_logger().info(f"tools.py saved: {result.tool_count} tools loaded")
            else:
                self.get_logger().warn(f"tools.py save failed: {result.message}")
        except Exception as e:
            import traceback
            self.get_logger().error(f"Set error: {e}")
            response.success = False
            response.message = f"Internal error: {e}\n{traceback.format_exc()}"
            response.tool_count = 0
        return response

    def handle_reload(self, request, response):
        try:
            count = registry.reload_tools()
            response.success = True
            response.tool_count = count
            self.get_logger().info(f"Reloaded {count} tools")
        except Exception as e:
            self.get_logger().error(f"Reload error: {e}")
            response.success = False
            response.tool_count = 0
        return response

    def handle_reset(self, request, response):
        try:
            count = registry.reset_tools()
            response.success = True
            response.tool_count = count
            self.get_logger().info(f"Reset: {count} tools restored")
        except Exception as e:
            self.get_logger().error(f"Reset error: {e}")
            response.success = False
            response.tool_count = 0
        return response


def main(args=None):
    rclpy.init(args=args)
    node = AgentNode()

    from rclpy.executors import MultiThreadedExecutor
    executor = MultiThreadedExecutor()
    executor.add_node(node)

    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
