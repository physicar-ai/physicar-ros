#!/usr/bin/env python3
"""
Agent Node - Tool management services

Services:
- /agent/tool/list   : List tools
- /agent/tool/get    : Get tool details / source code
- /agent/tool/call   : Run a tool
- /agent/tool/set    : Add or update a tool
- /agent/tool/delete : Delete a tool
- /agent/tool/reset  : Reset (restore builtins)

Topics:
- /agent/tool/reload : Tool-reload trigger (Empty)

System tools (reserved, cannot be registered):
- tool_list   : List tools
- tool_get    : Get tool details / source code
- tool_set    : Register or update a tool
- tool_delete : Delete a tool
- tool_reset  : Reset
"""

import json
import rclpy
from rclpy.node import Node
from std_msgs.msg import Empty

from physicar_interfaces.srv import (
    ToolList,
    ToolGet,
    ToolCall,
    ToolSet,
    ToolDelete,
    ToolReset,
)

from physicar_agent import registry
from physicar_agent.core import _AgentCore


class AgentNode(Node):
    def __init__(self):
        super().__init__('agent_node')

        # Bind AgentCore subscriptions to THIS node so rclpy.spin(self) ticks
        # all topic callbacks (otherwise the core's internal node is never spun
        # and `topic.raw()` returns the very first frame forever).
        _AgentCore(external_node=self)

        # Initialise and load tools
        tool_count = registry.init_tools()
        self.get_logger().info(f"Loaded {tool_count} tools into cache")
        
        # Create services
        self.list_srv = self.create_service(
            ToolList, '/agent/tool/list', self.handle_list
        )
        self.get_srv = self.create_service(
            ToolGet, '/agent/tool/get', self.handle_get
        )
        self.call_srv = self.create_service(
            ToolCall, '/agent/tool/call', self.handle_call
        )
        self.set_srv = self.create_service(
            ToolSet, '/agent/tool/set', self.handle_set
        )
        self.delete_srv = self.create_service(
            ToolDelete, '/agent/tool/delete', self.handle_delete
        )
        self.reset_srv = self.create_service(
            ToolReset, '/agent/tool/reset', self.handle_reset
        )
        
        # Subscribe to reload topic
        self.reload_sub = self.create_subscription(
            Empty, '/agent/tool/reload', self.handle_reload, 10
        )
        
        self.get_logger().info("Agent services ready")
    
    def handle_list(self, request, response):
        """Return tool list"""
        try:
            tools = registry.list_tools(include_system=request.include_system)
            response.tools_json = json.dumps(tools, ensure_ascii=False)
        except Exception as e:
            self.get_logger().error(f"List error: {e}")
            response.tools_json = json.dumps([])
        return response
    
    def handle_get(self, request, response):
        """Return tool details"""
        try:
            info = registry.get_tool_info(request.name)
            if info is None:
                response.found = False
                response.info_json = json.dumps({"error": f"Tool '{request.name}' not found"})
            else:
                response.found = True
                # If include_code is False and 'code' exists, drop it
                if not request.include_code and 'code' in info:
                    info = info.copy()
                    del info['code']
                response.info_json = json.dumps(info, ensure_ascii=False)
        except Exception as e:
            self.get_logger().error(f"Get error: {e}")
            response.found = False
            response.info_json = json.dumps({"error": str(e)})
        return response
    
    def handle_reload(self, msg):
        """Reload tools"""
        try:
            count = registry.reload_tools()
            self.get_logger().info(f"Reloaded {count} tools")
        except Exception as e:
            self.get_logger().error(f"Reload error: {e}")
    
    def _call_system_tool(self, name: str, args: dict) -> list:
        """Run a system tool"""
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
            tool_name = args.get("name")
            code = args.get("code")
            if not tool_name or not code:
                return [{"type": "text", "text": "Error: 'name' and 'code' are required"}]
            result = registry.set_tool(tool_name, code)
            msg = result.message
            if result.warnings:
                msg += f"\nWarnings: {', '.join(result.warnings)}"
            return [{"type": "text", "text": json.dumps({"success": result.success, "message": msg}, ensure_ascii=False)}]
        
        elif name == "tool_delete":
            tool_name = args.get("name")
            if not tool_name:
                return [{"type": "text", "text": "Error: 'name' is required"}]
            success = registry.delete_tool(tool_name)
            msg = f"Tool '{tool_name}' deleted" if success else f"Tool '{tool_name}' not found"
            return [{"type": "text", "text": json.dumps({"success": success, "message": msg}, ensure_ascii=False)}]
        
        elif name == "tool_reset":
            count = registry.reset_tools()
            return [{"type": "text", "text": json.dumps({"success": True, "tool_count": count}, ensure_ascii=False)}]
        
        else:
            return [{"type": "text", "text": f"Unknown system tool: {name}"}]
    
    def handle_call(self, request, response):
        """Run a tool"""
        try:
            args = json.loads(request.args_json) if request.args_json else {}
            
            # System tool
            if registry.is_system_tool(request.name):
                result = self._call_system_tool(request.name, args)
                response.success = True
                response.result_json = json.dumps(result, ensure_ascii=False)
            else:
                # Regular tool
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
        """Add or update a tool"""
        try:
            result = registry.set_tool(request.name, request.code)
            response.success = result.success
            response.message = result.message
            if result.warnings:
                response.message += f"\nWarnings: {', '.join(result.warnings)}"
            if result.success:
                self.get_logger().info(f"Tool '{request.name}' set successfully")
            else:
                self.get_logger().warn(f"Tool '{request.name}' set failed: {result.message}")
        except Exception as e:
            import traceback
            self.get_logger().error(f"Set error: {e}")
            response.success = False
            response.message = f"Internal error: {e}\n{traceback.format_exc()}"
        return response
    
    def handle_delete(self, request, response):
        """Delete a tool"""
        try:
            if registry.delete_tool(request.name):
                response.success = True
                response.message = f"Tool '{request.name}' deleted"
                self.get_logger().info(response.message)
            else:
                response.success = False
                response.message = f"Tool '{request.name}' not found"
        except Exception as e:
            self.get_logger().error(f"Delete error: {e}")
            response.success = False
            response.message = str(e)
        return response
    
    def handle_reset(self, request, response):
        """Reset"""
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
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
