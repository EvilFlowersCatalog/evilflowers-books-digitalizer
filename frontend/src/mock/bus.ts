/**
 * In-browser stand-in for `GET /api/v1/events`: fake EventSources that receive
 * the events the mock server emits, in the SSE shape (named event, id, JSON data).
 */

import type { EventSourceLike } from "../api/live";
import type { EventType, StudioEvent } from "../api/types";

type Listener = (ev: MessageEvent<string>) => void;

class FakeEventSource implements EventSourceLike {
  readyState = 0;
  onopen: ((ev: Event) => unknown) | null = null;
  onerror: ((ev: Event) => unknown) | null = null;
  private readonly listeners = new Map<string, Set<Listener>>();

  constructor(private readonly bus: MockBus) {
    bus.sources.add(this);
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.(new Event("open"));
    }, 200);
  }

  addEventListener(type: string, listener: Listener): void {
    const set = this.listeners.get(type) ?? new Set<Listener>();
    set.add(listener);
    this.listeners.set(type, set);
  }

  dispatch(type: string, data: string, id: string): void {
    const ev = new MessageEvent<string>(type, { data, lastEventId: id });
    this.listeners.get(type)?.forEach((fn) => fn(ev));
  }

  close(): void {
    this.readyState = 2;
    this.bus.sources.delete(this);
  }
}

export class MockBus {
  readonly sources = new Set<FakeEventSource>();
  private seq = 0;

  open = (): EventSourceLike => new FakeEventSource(this);

  emit(type: EventType, bookId: number | null, data: Record<string, unknown>, project = "stu-local"): void {
    const event: Omit<StudioEvent, "id"> = { type, project, book_id: bookId, data, ts: new Date().toISOString() };
    const id = String(++this.seq);
    this.sources.forEach((s) => s.dispatch(type, JSON.stringify(event), id));
  }
}
