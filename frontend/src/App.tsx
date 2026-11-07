/**
 * Routes. The page studio is lazy: it carries OpenSeadragon, Annotorious and KaTeX,
 * which the dashboard and the tables never need.
 */

import { lazy, Suspense, useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { connectEvents } from "./api/live";
import { Layout } from "./components/Layout";
import { Empty, Spinner } from "./components/ui";
import { BookScreen } from "./features/book/BookScreen";
import { BooksScreen } from "./features/books/BooksScreen";
import { DashboardScreen } from "./features/dashboard/DashboardScreen";
import { ReviewQueueScreen } from "./features/review/ReviewQueueScreen";
import { SettingsScreen } from "./features/settings/SettingsScreen";
import { SessionProvider } from "./lib/session";

const PageStudio = lazy(() => import("./features/studio/PageStudio"));

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { index: true, element: <DashboardScreen /> },
      { path: "books", element: <BooksScreen /> },
      { path: "books/:bookId", element: <BookScreen /> },
      {
        path: "pages/:pageId",
        element: (
          <Suspense fallback={<Empty><Spinner /> Loading the page studio…</Empty>}>
            <PageStudio />
          </Suspense>
        ),
      },
      { path: "review", element: <ReviewQueueScreen /> },
      { path: "settings", element: <SettingsScreen /> },
      { path: "*", element: <Empty>Nothing here.</Empty> },
    ],
  },
]);

export function App() {
  const client = useQueryClient();
  useEffect(() => connectEvents(client), [client]);
  return (
    <SessionProvider>
      <RouterProvider router={router} />
    </SessionProvider>
  );
}
