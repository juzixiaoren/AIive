import { WorkClient } from "./client";
import {
  errorMessage,
  type Bucket,
  type TimelineEvent,
  type WorkSummary,
  type WorkView,
} from "./types";

export interface WorkState {
  bucket: Bucket;
  items: WorkSummary[];
  cursor: string | null;
  more: boolean;
  selected: string;
  view: WorkView | null;
  timeline: TimelineEvent[];
  historyCursor: number;
  historyMore: boolean;
  loading: boolean;
  busy: boolean;
  error: string;
}
export function mergeTimeline(
  a: TimelineEvent[],
  b: TimelineEvent[],
): TimelineEvent[] {
  return [...new Map([...a, ...b].map((e) => [e.sequence, e])).values()].sort(
    (x, y) => x.sequence - y.sequence,
  );
}
/** Serial refreshes coalesce invalidations. Generation guards prevent responses crossing selections. */
export class WorkController {
  state: WorkState = {
    bucket: "attention",
    items: [],
    cursor: null,
    more: false,
    selected: "",
    view: null,
    timeline: [],
    historyCursor: 0,
    historyMore: false,
    loading: false,
    busy: false,
    error: "",
  };
  private listeners = new Set<() => void>();
  private generation = 0;
  private pending = false;
  private runner: Promise<void> | null = null;
  private disposed = false;
  private snapshot = 0;
  private seenChanges = new Map<
    string,
    { version: number; sequence: number }
  >();
  constructor(readonly api: WorkClient) {}
  start(): void {
    this.disposed = false;
    this.generation++;
  }
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): WorkState => this.state;
  invalidate(data: Record<string, unknown>): void {
    const id = typeof data.task_id === "string" ? data.task_id : "";
    const version = Number(data.task_version),
      sequence = Number(data.event_sequence);
    if (
      !id ||
      !Number.isSafeInteger(version) ||
      !Number.isSafeInteger(sequence)
    )
      return;
    const previous = this.seenChanges.get(id);
    if (
      previous &&
      previous.version >= version &&
      previous.sequence >= sequence
    )
      return;
    this.seenChanges.delete(id);
    this.seenChanges.set(id, {
      version: Math.max(version, previous?.version || 0),
      sequence: Math.max(sequence, previous?.sequence || 0),
    });
    if (this.seenChanges.size > 512)
      this.seenChanges.delete(this.seenChanges.keys().next().value!);
    void this.refresh();
  }
  private set(patch: Partial<WorkState>): void {
    if (!this.disposed) {
      this.state = { ...this.state, ...patch };
      this.listeners.forEach((f) => f());
    }
  }
  select(id: string): void {
    this.generation++;
    this.set({
      selected: id,
      view: null,
      timeline: [],
      historyCursor: 0,
      historyMore: false,
      error: "",
    });
    void this.refresh();
  }
  bucket(bucket: Bucket): void {
    this.generation++;
    this.set({ bucket, items: [], cursor: null, more: false });
    void this.refresh();
  }
  refresh = (): Promise<void> => {
    if (this.disposed) return Promise.resolve();
    this.pending = true;
    if (!this.runner)
      this.runner = this.drain().finally(() => {
        this.runner = null;
      });
    return this.runner;
  };
  private async drain(): Promise<void> {
    this.set({ loading: true });
    while (this.pending && !this.disposed) {
      this.pending = false;
      const generation = this.generation,
        selected = this.state.selected;
      try {
        const [inbox, view] = await Promise.all([
          this.api.inbox(this.state.bucket),
          selected ? this.api.view(selected) : Promise.resolve(null),
        ]);
        if (generation !== this.generation || this.disposed) continue;
        // Replace, rather than retain, cached content so Forget also removes old timeline entries.
        this.snapshot++;
        this.set({
          items: inbox.items,
          cursor: inbox.next_cursor,
          more: inbox.has_more,
          view,
          timeline: view?.timeline || [],
          historyCursor: 0,
          historyMore: Boolean(view?.truncated.timeline),
          error: "",
        });
        if (view) {
          let after = view.last_sequence;
          // Bound catch-up work under continuously arriving events. Next invalidation resumes from a new snapshot.
          for (let page = 0; page < 5; page++) {
            const delta = await this.api.timeline(selected, after);
            if (generation !== this.generation || this.disposed) break;
            this.set({
              timeline: mergeTimeline(this.state.timeline, delta.items),
            });
            if (!delta.has_more || delta.next_sequence <= after) break;
            after = delta.next_sequence;
          }
        }
      } catch (error) {
        if (generation === this.generation)
          this.set({
            error: errorMessage(error),
            view: null,
            timeline: [],
            items: [],
            cursor: null,
            more: false,
          });
      }
    }
    this.set({ loading: false });
  }
  async more(): Promise<void> {
    if (!this.state.cursor || this.state.loading) return;
    const generation = this.generation,
      snapshot = this.snapshot;
    this.set({ loading: true });
    try {
      const page = await this.api.inbox(this.state.bucket, this.state.cursor);
      if (generation === this.generation && snapshot === this.snapshot)
        this.set({
          items: [
            ...new Map(
              [...this.state.items, ...page.items].map((i) => [i.task_id, i]),
            ).values(),
          ],
          cursor: page.next_cursor,
          more: page.has_more,
        });
    } catch (error) {
      if (generation === this.generation)
        this.set({ error: errorMessage(error) });
    } finally {
      if (!this.runner) this.set({ loading: false });
    }
  }
  async history(): Promise<void> {
    if (!this.state.selected || this.state.loading) return;
    const generation = this.generation,
      snapshot = this.snapshot;
    this.set({ loading: true });
    try {
      const page = await this.api.timeline(
        this.state.selected,
        this.state.historyCursor,
      );
      if (generation === this.generation && snapshot === this.snapshot)
        this.set({
          timeline: mergeTimeline(this.state.timeline, page.items),
          historyCursor: page.next_sequence,
          historyMore: page.has_more,
        });
    } catch (error) {
      if (generation === this.generation)
        this.set({ error: errorMessage(error) });
    } finally {
      if (!this.runner) this.set({ loading: false });
    }
  }
  async act(operation: () => Promise<unknown>): Promise<boolean> {
    if (this.state.busy) return false;
    this.set({ busy: true, error: "" });
    let succeeded = false,
      failure = "";
    try {
      await operation();
      succeeded = true;
    } catch (error) {
      failure = errorMessage(error);
    }
    // Also reconcile conflicts and ambiguous network failures; never optimistically change task state.
    await this.refresh();
    this.set({ busy: false, ...(failure ? { error: failure } : {}) });
    return succeeded;
  }
  dispose(): void {
    this.disposed = true;
    this.generation++;
    this.listeners.clear();
  }
}
