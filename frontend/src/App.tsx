import ChatPage from "./pages/ChatPage";

export default function App() {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-4">
      <header className="mb-6 text-center">
        <h1 className="text-2xl font-bold tracking-tight">AIive</h1>
        <p className="text-sm text-gray-500">Personal Agent</p>
      </header>
      <main className="w-full max-w-2xl">
        <ChatPage />
      </main>
    </div>
  );
}
