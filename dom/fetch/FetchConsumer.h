/* -*- Mode: C++; tab-width: 8; indent-tabs-mode: nil; c-basic-offset: 2 -*- */
/* vim: set ts=8 sts=2 et sw=2 tw=80: */
/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

#ifndef mozilla_dom_FetchConsumer_h
#define mozilla_dom_FetchConsumer_h

#include "Fetch.h"
#include "mozilla/dom/AbortSignal.h"
#include "nsIObserver.h"
#include "nsWeakReference.h"

class nsIThread;

namespace mozilla {
namespace dom {

class Promise;

namespace workers {
class WorkerPrivate;
class WorkerHolder;
}

template <class Derived> class FetchBody;

// FetchBody is not thread-safe but we need to move it around threads.
// In order to keep it alive all the time, we use a WorkerHolder, if created on
// workers, plus a this consumer.
template <class Derived>
class FetchBodyConsumer final : public nsIObserver
                              , public nsSupportsWeakReference
                              , public AbortFollower
{
public:
  NS_DECL_THREADSAFE_ISUPPORTS
  NS_DECL_NSIOBSERVER

  static already_AddRefed<Promise>
  Create(nsIGlobalObject* aGlobal,
         nsIEventTarget* aMainThreadEventTarget,
         FetchBody<Derived>* aBody,
         AbortSignal* aSignal,
         FetchConsumeType aType,
         ErrorResult& aRv);

  void
  ReleaseObject();

  FetchBody<Derived>*
  Body() const
  {
    return mBody;
  }

  void
  BeginConsumeBodyMainThread();

  void
  ContinueConsumeBody(nsresult aStatus, uint32_t aLength, uint8_t* aResult);

  void
  ContinueConsumeBlobBody(BlobImpl* aBlobImpl);

  void
  ShutDownMainThreadConsuming();

  workers::WorkerPrivate*
  GetWorkerPrivate() const
  {
    return mWorkerPrivate;
  }

  void
  NullifyConsumeBodyPump()
  {
    mConsumeBodyPump = nullptr;
  }

  // AbortFollower
  void Abort() override;

private:
  FetchBodyConsumer(nsIEventTarget* aMainThreadEventTarget,
                    nsIGlobalObject* aGlobalObject,
                    workers::WorkerPrivate* aWorkerPrivate,
                    FetchBody<Derived>* aBody,
                    Promise* aPromise,
                    FetchConsumeType aType);

  ~FetchBodyConsumer();

  void
  AssertIsOnTargetThread() const;

  bool
  RegisterWorkerHolder(workers::WorkerPrivate* aWorkerPrivate);

  nsCOMPtr<nsIThread> mTargetThread;
  nsCOMPtr<nsIEventTarget> mMainThreadEventTarget;
  RefPtr<FetchBody<Derived>> mBody;

  // Set when consuming the body is attempted on a worker.
  // Unset when consumption is done/aborted.
  // This WorkerHolder keeps alive the consumer via a cycle.
  UniquePtr<workers::WorkerHolder> mWorkerHolder;

  nsCOMPtr<nsIGlobalObject> mGlobal;

  // Always set whenever the FetchBodyConsumer is created on the worker thread.
  workers::WorkerPrivate* mWorkerPrivate;

  // Touched on the main-thread only.
  nsCOMPtr<nsIInputStreamPump> mConsumeBodyPump;

  // Only ever set once, always on target thread.
  FetchConsumeType mConsumeType;
  RefPtr<Promise> mConsumePromise;

  // touched only on the target thread.
  bool mBodyConsumed;

  // touched only on the main-thread.
  bool mShuttingDown;
};

} // namespace dom
} // namespace mozilla

#endif // mozilla_dom_FetchConsumer_h
