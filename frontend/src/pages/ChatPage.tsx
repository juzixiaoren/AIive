import { useState } from "react";
import { sendMessage, ChatResponse } from "../api/chat";

interface Message {
  role: "user" | "agent";
  content: string;
  traceId: string;
  threadId: string;
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [threadId, setThreadId] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || loading) return;

    setInput("");
    setError(null);
    setLoading(true);

    const userMsg: Message = {
      role: "user",
      content: text,
      traceId: "",
      threadId: threadId ?? "(new)",
    };
    setMessages((prev) => [...prev, userMsg]);

    try {
      const data: ChatResponse = await sendMessage(text, threadId);

      if (!threadId) {
        setThreadId(data.thread_id);
      }

      const agentMsg: Message = {
        role: "agent",
        content: data.reply,
        traceId: data.trace_id,
        threadId: data.thread_id,
      };
      setMessages((prev) => [...prev, agentMsg]);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Messages */}
      <div className="flex flex-col gap-3 min-h-[400px] max-h-[60vh] overflow-y-auto rounded-lg border border-gray-200 bg-gray-50 p-4">
        {messages.length === 0 && (
          <p className="text-gray-400 text-sm text-center mt-20">
            Send a message to start a conversation.
          </p>
        )}
        {messages.map((msg, i) => (
          <div
            key={i}
            className={`flex flex-col ${msg.role === "user" ? "items-end" : "items-start"}`}
          >
            <div
              className={`max-w-[80%] rounded-lg px-4 py-2 text-sm whitespace-pre-wrap ${
                msg.role === "user"
                  ? "bg-blue-500 text-white"
                  : "bg-white border border-gray-200 text-gray-900"
              }`}
            >
              {msg.content}
            </div>
            <span className="text-[10px] text-gray-400 mt-0.5 px-1">
              {msg.role === "agent"
                ? `trace: ${msg.traceId.slice(0, 8)}… | thread: ${msg.threadId.slice(0, 8)}…`
                : `thread: ${msg.threadId}`}
            </span>
          </div>
        ))}
        {loading && (
          <div className="self-start bg-gray-100 rounded-lg px-4 py-2 text-sm text-gray-500 animate-pulse">
            Thinking...
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-600">
          {error}
        </div>
      )}

      {/* Input */}
      <div className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type a message..."
          disabled={loading}
          className="flex-1 rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
        />
        <button
          onClick={handleSend}
          disabled={loading || !input.trim()}
          className="rounded-lg bg-blue-500 px-6 py-2 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          Send
        </button>
      </div>
    </div>
  );
}
