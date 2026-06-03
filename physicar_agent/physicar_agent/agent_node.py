#!/usr/bin/env python3
"""
Agent Node - Tool management services

Services:
- /agent/tool/list   : List tools
- /agent/tool/get    : Get tool details / source code
- /agent/tool/call   : Run a tool
- /agent/tool/set    : Save entire tools.py and load
- /agent/tool/load   : Load tools from disk
- /agent/tool/init   : Delete tools.py, load builtin

System tools (reserved, cannot be used as function names):
- tool_list  : List tools
- tool_get   : Get tool details / source code
- tool_set   : Save entire tools.py
- tool_load  : Load tools from disk
- tool_init  : Init to builtin
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
    ToolLoad,
    ToolInit,
)

from physicar_agent import registry


class AgentNode(Node):
    def __init__(self):
        super().__init__('agent_node')

        result = registry.load_tools()
        self.get_logger().info(f"Loaded {result.tool_count} tools (success={result.success})")

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
        self.load_srv = self.create_service(
            ToolLoad, '/agent/tool/load', self.handle_load,
            callback_group=self._srv_cb_group
        )
        self.init_srv = self.create_service(
            ToolInit, '/agent/tool/init', self.handle_init,
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
            if not request.name:
                # Empty name → return full source code
                code = registry.get_tool_code()
                if code is not None:
                    response.found = True
                    response.info_json = json.dumps({"code": code}, ensure_ascii=False)
                else:
                    response.found = False
                    response.info_json = json.dumps({"error": "No tools loaded"})
            else:
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

        elif name == "tool_code":
            code = registry.get_tool_code()
            if code is None:
                return [{"type": "text", "text": "No tools loaded"}]
            return [{"type": "text", "text": code}]

        elif name == "tool_set":
            code = args.get("code")
            if not code:
                return [{"type": "text", "text": "Error: 'code' is required"}]
            result = registry.set_tools(code)
            return [{"type": "text", "text": json.dumps({"success": result.success, "message": result.message, "tool_count": result.tool_count}, ensure_ascii=False)}]

        elif name == "tool_load":
            result = registry.load_tools()
            return [{"type": "text", "text": json.dumps({"success": result.success, "message": result.message, "tool_count": result.tool_count}, ensure_ascii=False)}]

        elif name == "tool_init":
            result = registry.init_tools()
            return [{"type": "text", "text": json.dumps({"success": result.success, "message": result.message, "tool_count": result.tool_count}, ensure_ascii=False)}]

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

    def handle_load(self, request, response):
        try:
            result = registry.load_tools()
            response.success = result.success
            response.tool_count = result.tool_count
            if result.success:
                self.get_logger().info(f"Loaded {result.tool_count} tools")
            else:
                self.get_logger().warn(f"Load failed: {result.message}")
        except Exception as e:
            self.get_logger().error(f"Load error: {e}")
            response.success = False
            response.tool_count = 0
        return response

    def handle_init(self, request, response):
        try:
            result = registry.init_tools()
            response.success = result.success
            response.tool_count = result.tool_count
            if result.success:
                self.get_logger().info(f"Init: {result.tool_count} tools loaded")
            else:
                self.get_logger().warn(f"Init failed: {result.message}")
        except Exception as e:
            self.get_logger().error(f"Init error: {e}")
            response.success = False
            response.tool_count = 0
        return response


def main(args=None):
    rclpy.init(args=args)
    node = AgentNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
