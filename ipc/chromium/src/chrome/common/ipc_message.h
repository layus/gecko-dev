/* -*- Mode: C++; tab-width: 8; indent-tabs-mode: nil; c-basic-offset: 2 -*- */
/* vim: set ts=8 sts=2 et sw=2 tw=80: */
// Copyright (c) 2006-2008 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef CHROME_COMMON_IPC_MESSAGE_H__
#define CHROME_COMMON_IPC_MESSAGE_H__

#include <string>

#include "base/basictypes.h"
#include "base/pickle.h"
#include "mozilla/TimeStamp.h"

#ifdef MOZ_TASK_TRACER
#include "GeckoTaskTracer.h"
#endif

#if defined(OS_POSIX)
#include "nsAutoPtr.h"
#endif

namespace base {
struct FileDescriptor;
}

class FileDescriptorSet;

namespace IPC {

//------------------------------------------------------------------------------

class Channel;
class Message;
struct LogData;

class Message : public Pickle {
 public:
  typedef uint32_t msgid_t;

  enum NestedLevel {
    NOT_NESTED = 1,
    NESTED_INSIDE_SYNC = 2,
    NESTED_INSIDE_CPOW = 3
  };

  enum PriorityValue {
    NORMAL_PRIORITY = 0,
    INPUT_PRIORITY = 1,
    HIGH_PRIORITY = 2,
  };

  enum MessageCompression {
    COMPRESSION_NONE,
    COMPRESSION_ENABLED,
    COMPRESSION_ALL
  };

  virtual ~Message();

  Message();

  // Initialize a message with a user-defined type, priority value, and
  // destination WebView ID.
  //
  // NOTE: `recordWriteLatency` is only passed by IPDL generated message code,
  // and is used to trigger the IPC_WRITE_LATENCY_MS telemetry.
  Message(int32_t routing_id,
          msgid_t type,
          uint32_t segmentCapacity = 0, // 0 for the default capacity.
          NestedLevel nestedLevel = NOT_NESTED,
          PriorityValue priority = NORMAL_PRIORITY,
          MessageCompression compression = COMPRESSION_NONE,
          const char* const name="???",
          bool recordWriteLatency=false);

  Message(const char* data, int data_len);

  Message(const Message& other) = delete;
  Message(Message&& other);
  Message& operator=(const Message& other) = delete;
  Message& operator=(Message&& other);

  NestedLevel nested_level() const {
    return static_cast<NestedLevel>(header()->flags & NESTED_MASK);
  }

  void set_nested_level(NestedLevel nestedLevel) {
    DCHECK((nestedLevel & ~NESTED_MASK) == 0);
    header()->flags = (header()->flags & ~NESTED_MASK) | nestedLevel;
  }

  PriorityValue priority() const {
    return static_cast<PriorityValue>((header()->flags & PRIO_MASK) >> 2);
  }

  void set_priority(PriorityValue prio) {
    DCHECK(((prio << 2) & ~PRIO_MASK) == 0);
    header()->flags = (header()->flags & ~PRIO_MASK) | (prio << 2);
  }

  bool is_constructor() const {
    return (header()->flags & CONSTRUCTOR_BIT) != 0;
  }

  void set_constructor() {
    header()->flags |= CONSTRUCTOR_BIT;
  }

  // True if this is a synchronous message.
  bool is_sync() const {
    return (header()->flags & SYNC_BIT) != 0;
  }

  // True if this is a synchronous message.
  bool is_interrupt() const {
    return (header()->flags & INTERRUPT_BIT) != 0;
  }

  // True if compression is enabled for this message.
  MessageCompression compress_type() const {
    return (header()->flags & COMPRESS_BIT) ?
               COMPRESSION_ENABLED :
               (header()->flags & COMPRESSALL_BIT) ?
                   COMPRESSION_ALL :
                   COMPRESSION_NONE;
  }

  // Set this on a reply to a synchronous message.
  void set_reply() {
    header()->flags |= REPLY_BIT;
  }

  bool is_reply() const {
    return (header()->flags & REPLY_BIT) != 0;
  }

  // Set this on a reply to a synchronous message to indicate that no receiver
  // was found.
  void set_reply_error() {
    header()->flags |= REPLY_ERROR_BIT;
  }

  bool is_reply_error() const {
    return (header()->flags & REPLY_ERROR_BIT) != 0;
  }

  msgid_t type() const {
    return header()->type;
  }

  int32_t routing_id() const {
    return header()->routing;
  }

  void set_routing_id(int32_t new_id) {
    header()->routing = new_id;
  }

  int32_t transaction_id() const {
    return header()->txid;
  }

  void set_transaction_id(int32_t txid) {
    header()->txid = txid;
  }

  uint32_t interrupt_remote_stack_depth_guess() const {
    return header()->interrupt_remote_stack_depth_guess;
  }

  void set_interrupt_remote_stack_depth_guess(uint32_t depth) {
    DCHECK(is_interrupt());
    header()->interrupt_remote_stack_depth_guess = depth;
  }

  uint32_t interrupt_local_stack_depth() const {
    return header()->interrupt_local_stack_depth;
  }

  void set_interrupt_local_stack_depth(uint32_t depth) {
    DCHECK(is_interrupt());
    header()->interrupt_local_stack_depth = depth;
  }

  int32_t seqno() const {
    return header()->seqno;
  }

  void set_seqno(int32_t aSeqno) {
    header()->seqno = aSeqno;
  }

  const char* name() const {
    return name_;
  }

  void set_name(const char* const aName) {
    name_ = aName;
  }

  const mozilla::TimeStamp& create_time() const {
    return create_time_;
  }

#if defined(OS_POSIX)
  uint32_t num_fds() const;
#endif

  template<class T>
  static bool Dispatch(const Message* msg, T* obj, void (T::*func)()) {
    (obj->*func)();
    return true;
  }

  template<class T>
  static bool Dispatch(const Message* msg, T* obj, void (T::*func)() const) {
    (obj->*func)();
    return true;
  }

  template<class T>
  static bool Dispatch(const Message* msg, T* obj,
                       void (T::*func)(const Message&)) {
    (obj->*func)(*msg);
    return true;
  }

  template<class T>
  static bool Dispatch(const Message* msg, T* obj,
                       void (T::*func)(const Message&) const) {
    (obj->*func)(*msg);
    return true;
  }

  // Used for async messages with no parameters.
  static void Log(const Message* msg, std::wstring* l) {
  }

  static int HeaderSizeFromData(const char* range_start,
                                const char* range_end) {
#ifdef MOZ_TASK_TRACER
    return ((static_cast<unsigned int>(range_end - range_start) >= sizeof(Header)) &&
            (reinterpret_cast<const Header*>(range_start)->flags &
             TASKTRACER_BIT)) ?
      sizeof(HeaderTaskTracer) : sizeof(Header);
#else
    return sizeof(Header);
#endif
  }

  // Figure out how big the message starting at range_start is. Returns 0 if
  // there's no enough data to determine (i.e., if [range_start, range_end) does
  // not contain enough of the message header to know the size).
  static uint32_t MessageSize(const char* range_start, const char* range_end) {
    return Pickle::MessageSize(HeaderSizeFromData(range_start, range_end),
                               range_start, range_end);
  }

#if defined(OS_POSIX)
  // On POSIX, a message supports reading / writing FileDescriptor objects.
  // This is used to pass a file descriptor to the peer of an IPC channel.

  // Add a descriptor to the end of the set. Returns false iff the set is full.
  bool WriteFileDescriptor(const base::FileDescriptor& descriptor);
  // Get a file descriptor from the message. Returns false on error.
  //   iter: a Pickle iterator to the current location in the message.
  bool ReadFileDescriptor(PickleIterator* iter, base::FileDescriptor* descriptor) const;

#if defined(OS_MACOSX)
  void set_fd_cookie(uint32_t cookie) {
    header()->cookie = cookie;
  }
  uint32_t fd_cookie() const {
    return header()->cookie;
  }
#endif
#endif


  friend class Channel;
  friend class MessageReplyDeserializer;
  friend class SyncMessage;

  void set_sync() {
    header()->flags |= SYNC_BIT;
  }

  void set_interrupt() {
    header()->flags |= INTERRUPT_BIT;
  }

#ifdef MOZ_TASK_TRACER
  void TaskTracerDispatch();
  class AutoTaskTracerRun
    : public mozilla::tasktracer::AutoSaveCurTraceInfo {
    Message& mMsg;
    uint64_t mTaskId;
    uint64_t mSourceEventId;
  public:
    explicit AutoTaskTracerRun(Message& aMsg);
    ~AutoTaskTracerRun();
  };
#endif

#if !defined(OS_MACOSX)
 protected:
#endif

  // flags
  enum {
    NESTED_MASK     = 0x0003,
    PRIO_MASK       = 0x000C,
    SYNC_BIT        = 0x0010,
    REPLY_BIT       = 0x0020,
    REPLY_ERROR_BIT = 0x0040,
    INTERRUPT_BIT   = 0x0080,
    COMPRESS_BIT    = 0x0100,
    COMPRESSALL_BIT = 0x0200,
    CONSTRUCTOR_BIT = 0x0400,
#ifdef MOZ_TASK_TRACER
    TASKTRACER_BIT  = 0x0800,
#endif
  };

  struct Header : Pickle::Header {
    int32_t routing;  // ID of the view that this message is destined for
    msgid_t type;   // specifies the user-defined message type
    uint32_t flags;   // specifies control flags for the message
#if defined(OS_POSIX)
    uint32_t num_fds; // the number of descriptors included with this message
# if defined(OS_MACOSX)
    uint32_t cookie;  // cookie to ACK that the descriptors have been read.
# endif
#endif
    union {
      // For Interrupt messages, a guess at what the *other* side's stack depth is.
      uint32_t interrupt_remote_stack_depth_guess;

      // For RPC and Urgent messages, a transaction ID for message ordering.
      int32_t txid;
    };
    // The actual local stack depth.
    uint32_t interrupt_local_stack_depth;
    // Sequence number
    int32_t seqno;
  };

#ifdef MOZ_TASK_TRACER
  /**
   * The type is used as headers of Messages only if TaskTracer is
   * enabled, or type |Header| would be used instead.
   */
  struct HeaderTaskTracer : public Header {
    uint64_t task_id;
    uint64_t source_event_id;
    uint64_t parent_task_id;
    mozilla::tasktracer::SourceEventType source_event_type;
  };
#endif

#ifdef MOZ_TASK_TRACER
  bool UseTaskTracerHeader() const {
    return sizeof(HeaderTaskTracer) == (size() - payload_size());
  }

  Header* header() {
    return UseTaskTracerHeader() ?
      headerT<HeaderTaskTracer>() : headerT<Header>();
  }
  const Header* header() const {
    return UseTaskTracerHeader() ?
      headerT<HeaderTaskTracer>() : headerT<Header>();
  }
#else
  Header* header() {
    return headerT<Header>();
  }
  const Header* header() const {
    return headerT<Header>();
  }
#endif

  void InitLoggingVariables(const char* const name="???");

#if defined(OS_POSIX)
  // The set of file descriptors associated with this message.
  RefPtr<FileDescriptorSet> file_descriptor_set_;

  // Ensure that a FileDescriptorSet is allocated
  void EnsureFileDescriptorSet();

  FileDescriptorSet* file_descriptor_set() {
    EnsureFileDescriptorSet();
    return file_descriptor_set_.get();
  }
  const FileDescriptorSet* file_descriptor_set() const {
    return file_descriptor_set_.get();
  }
#endif

  const char* name_;

  mozilla::TimeStamp create_time_;

};

class MessageInfo {
public:
    typedef uint32_t msgid_t;

    explicit MessageInfo(const Message& aMsg)
        : mSeqno(aMsg.seqno()), mType(aMsg.type()) {}

    int32_t seqno() const { return mSeqno; }
    msgid_t type() const { return mType; }

private:
    int32_t mSeqno;
    msgid_t mType;
};

//------------------------------------------------------------------------------

}  // namespace IPC

enum SpecialRoutingIDs {
  // indicates that we don't have a routing ID yet.
  MSG_ROUTING_NONE = kint32min,

  // indicates a general message not sent to a particular tab.
  MSG_ROUTING_CONTROL = kint32max
};

#define IPC_REPLY_ID 0xFFF0  // Special message id for replies
#define IPC_LOGGING_ID 0xFFF1  // Special message id for logging

#endif  // CHROME_COMMON_IPC_MESSAGE_H__
