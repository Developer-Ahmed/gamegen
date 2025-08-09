import React, { useState, useRef, useEffect } from 'react';
import { Send, Bot, User, Settings, /* Mic, */ Plus, Trash2, Menu, X, Edit3, Check, Paperclip, XCircle, MessageSquarePlus } from 'lucide-react'; // Mic removed from imports

const AppleChatbot = () => {
  const initialBotMessage = {
    id: 1,
    type: 'bot',
    content: "Hello! I'm your AI assistant, powered by Gemini. How can I help you today? You can also send images!",
    timestamp: new Date()
  };

  const [messages, setMessages] = useState([initialBotMessage]);
  const [inputValue, setInputValue] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const [conversations, setConversations] = useState([
    { id: 'default_conv_1', title: 'New Conversation', messages: [initialBotMessage], isActive: true, createdAt: Date.now() } 
  ]);
  const [activeConversationId
  , setActiveConversationId] = useState('default_conv_1');
  
  const [editingConversationId, setEditingConversationId] = useState(null); 
  const [editingTitle, setEditingTitle] = useState(''); 
  const [isEditingInHeader, setIsEditingInHeader] = useState(false); 

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);
  const titleInputRef = useRef(null);  
  const headerTitleInputRef = useRef(null); 
  const imageInputRef = useRef(null); 

  const [selectedImage, setSelectedImage] = useState(null); 


  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    const activeConv = conversations.find(c => c.id === activeConversationId);
    if (activeConv) {
      setMessages(activeConv.messages);
      if (isEditingInHeader && editingConversationId !== activeConv.id) { 
        setIsEditingInHeader(false); 
        setEditingConversationId(null);
      }
    } else if (conversations.length > 0) {
      setActiveConversationId(conversations[0].id);
    } else {
      startNewConversation(false); 
    }
  }, [activeConversationId, conversations, isEditingInHeader, editingConversationId]);

  useEffect(() => {
    if (editingConversationId && titleInputRef.current && !isEditingInHeader) {
      titleInputRef.current.focus();
      titleInputRef.current.select(); 
    }
    if (editingConversationId && headerTitleInputRef.current && isEditingInHeader) {
      headerTitleInputRef.current.focus();
      headerTitleInputRef.current.select();
    }
  }, [editingConversationId, isEditingInHeader]);


  const callLLMAPI = async (userMessageContent, conversationHistory, imageBase64Data = null, imageMimeType = null) => {
    setIsLoading(true);
    const apiKey = ""; 

    const currentUserParts = [];
    if (userMessageContent) {
        currentUserParts.push({ text: userMessageContent });
    }
    if (imageBase64Data && imageMimeType) {
        currentUserParts.push({
            inlineData: {
                mimeType: imageMimeType,
                data: imageBase64Data
            }
        });
    }

    if (currentUserParts.length === 0) {
        setIsLoading(false);
        return "Please provide a message or an image.";
    }

    const apiContents = conversationHistory
      .filter(msg => msg.id !== (userMessageContent?.id || null)) 
      .map(msg => {
        const parts = [];
        if (msg.content || (msg.type === 'user' && !msg.imagePreviewUrl) ) parts.push({ text: msg.content || "" }); 
        
        if (msg.imagePreviewUrl && msg.type === 'user' && msg.base64Data && msg.mimeType) {
           if(!msg.content) parts.push({text: ""}); 
           parts.push({ inlineData: { mimeType: msg.mimeType, data: msg.base64Data }});
        } else if (msg.imagePreviewUrl && msg.type === 'user' && (!msg.base64Data || !msg.mimeType)){
            console.warn("User message in history has image preview but no base64/mime data for API.");
        }
        return { role: msg.type === 'user' ? 'user' : 'model', parts };
      });
    
    if (currentUserParts.length > 0) {
        apiContents.push({ role: "user", parts: currentUserParts });
    }


    const payload = {
      contents: apiContents.filter(content => content.parts.length > 0), 
      generationConfig: {
        temperature: 0.7,
        topK: 1,
        topP: 1,
        maxOutputTokens: 2048,
      },
       safetySettings: [
        { category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_MEDIUM_AND_ABOVE' },
        { category: 'HARM_CATEGORY_HATE_SPEECH', threshold: 'BLOCK_MEDIUM_AND_ABOVE' },
        { category: 'HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold: 'BLOCK_MEDIUM_AND_ABOVE' },
        { category: 'HARM_CATEGORY_DANGEROUS_CONTENT', threshold: 'BLOCK_MEDIUM_AND_ABOVE' },
      ],
    };

    try {
      const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${apiKey}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorData = await response.json();
        console.error("API Error Response:", errorData);
        let errorMessage = `API request failed with status ${response.status}.`;
        if (errorData.error && errorData.error.message) {
            errorMessage += ` Message: ${errorData.error.message}`;
        }
         if (response.status === 400 && errorData?.error?.message?.includes("prompt was blocked")) {
            errorMessage = "Your message was blocked by the safety filter. Please rephrase your message or remove the image.";
        } else if (response.status === 400 && errorData?.error?.message?.includes("must have alternating User and Model roles")) {
            errorMessage = "There was an issue with the conversation flow. Please try starting a new conversation or rephrasing.";
        } else if (response.status === 403) { 
             errorMessage = "API Authentication Error: Could not verify API key. Please ensure it's correctly configured in the environment.";
        } else if (response.status === 400 && errorData?.error?.message?.includes("Invalid inline data")) {
            errorMessage = "The uploaded image could not be processed. Please try a different image or format (e.g., JPEG, PNG).";
        }
        throw new Error(errorMessage);
      }

      const data = await response.json();

      if (data.candidates && data.candidates.length > 0 &&
          data.candidates[0].content && data.candidates[0].content.parts &&
          data.candidates[0].content.parts.length > 0) {
        return data.candidates[0].content.parts[0].text;
      } else if (data.promptFeedback && data.promptFeedback.blockReason) {
        console.warn("Prompt blocked:", data.promptFeedback);
        return `I'm sorry, but your request was blocked due to: ${data.promptFeedback.blockReason}. Please try rephrasing or removing the image.`;
      } else {
        console.warn("Unexpected API response structure:", data);
        return "I received a response, but couldn't understand it. Please try again.";
      }

    } catch (error) {
      console.error('Failed to get response from AI service:', error);
      return error.message || "I apologize, but I'm having trouble connecting to the AI service right now. Please check your connection or try again later.";
    } finally {
      setIsLoading(false);
    }
  };

  const handleSendMessage = async () => {
    const trimmedInput = inputValue.trim();
    const userMessageObject = {
        id: Date.now(), 
        content: trimmedInput,
        imagePreviewUrl: selectedImage ? selectedImage.previewUrl : null,
        base64Data: selectedImage ? selectedImage.base64Data : null,
        mimeType: selectedImage ? selectedImage.mimeType : null,
        type: 'user', 
        timestamp: new Date() 
    };

    if ((!trimmedInput && !selectedImage) || isLoading) return;

    const displayedUserMessage = {
      ...userMessageObject, 
      id: Date.now(), 
    };

    const updatedMessages = [...messages, displayedUserMessage];
    setMessages(updatedMessages);
    setConversations(prevConvs =>
      prevConvs.map(conv =>
        conv.id === activeConversationId ? { ...conv, messages: updatedMessages } : conv
      )
    );
    
    const imageBase64ForAPI = selectedImage ? selectedImage.base64Data : null;
    const imageMimeForAPI = selectedImage ? selectedImage.mimeType : null;

    setInputValue('');
    setSelectedImage(null); 

    try {
      const currentConversation = conversations.find(c => c.id === activeConversationId);
      const historyForAPI = currentConversation ? currentConversation.messages.filter(m => m.id !== displayedUserMessage.id) : [];
      
      const response = await callLLMAPI(trimmedInput, historyForAPI, imageBase64ForAPI, imageMimeForAPI);
      const botMessage = {
        id: Date.now() + 1,
        type: 'bot',
        content: response,
        timestamp: new Date()
      };
      
      const finalMessages = [...updatedMessages, botMessage];
      setMessages(finalMessages);
      setConversations(prevConvs =>
        prevConvs.map(conv =>
          conv.id === activeConversationId ? { ...conv, messages: finalMessages } : conv
        )
      );

    } catch (error) { 
      const errorMessageContent = error.message || "An unexpected error occurred. Please try again.";
      const errorMessage = {
        id: Date.now() + 1,
        type: 'bot',
        content: errorMessageContent,
        timestamp: new Date()
      };
      const finalMessages = [...updatedMessages, errorMessage];
      setMessages(finalMessages);
      setConversations(prevConvs =>
        prevConvs.map(conv =>
          conv.id === activeConversationId ? { ...conv, messages: finalMessages } : conv
        )
      );
    }
  };

  const handleImageFileChange = (event) => {
    const file = event.target.files[0];
    if (file) {
      if (file.size > 4 * 1024 * 1024) { 
        alert("Image size exceeds 4MB limit. Please choose a smaller image.");
        return;
      }
      const reader = new FileReader();
      reader.onloadend = () => {
        setSelectedImage({
          file: file,
          previewUrl: URL.createObjectURL(file), 
          base64Data: reader.result.split(',')[1], 
          mimeType: file.type
        });
      };
      reader.readAsDataURL(file);
    }
    event.target.value = null; 
  };

  const removeSelectedImage = () => {
    if (selectedImage && selectedImage.previewUrl) {
      URL.revokeObjectURL(selectedImage.previewUrl); 
    }
    setSelectedImage(null);
  };


  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  const formatTime = (date) => {
    if (!(date instanceof Date) || isNaN(date)) {
        return 'Invalid date';
    }
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  const startNewConversation = (deactivateOthers = true) => {
    if (editingConversationId) { 
        handleSaveTitle(editingConversationId, editingTitle);
    }
    const newConvId = `conv_${Date.now()}`;
    const newBotMessage = {
        id: Date.now(), 
        type: 'bot',
        content: "New conversation started. How can I assist you? Feel free to send images too!",
        timestamp: new Date()
    };
    const newConv = {
      id: newConvId,
      title: `Conversation ${conversations.length + 1}`, 
      messages: [newBotMessage],
      isActive: true,
      createdAt: Date.now() 
    };

    setConversations(prev =>
      [newConv, ...(deactivateOthers ? prev.map(c => ({ ...c, isActive: false })) : prev)]
    );
    setActiveConversationId(newConvId); 
    setInputValue(''); 
    removeSelectedImage(); 
    if (inputRef.current) {
        inputRef.current.focus();
    }
    if (sidebarOpen) { 
        setSidebarOpen(false);
    }
  };

  const selectConversation = (convId) => {
    if (convId === activeConversationId && !editingConversationId && !isEditingInHeader) return; 
    if (editingConversationId) { 
        handleSaveTitle(editingConversationId, editingTitle); 
    }
    setIsEditingInHeader(false); 
    setEditingConversationId(null); 
    setConversations(prev => prev.map(c => ({...c, isActive: c.id === convId})));
    setActiveConversationId(convId);
    removeSelectedImage(); 
    if (inputRef.current) {
        inputRef.current.focus();
    }
  };

  const handleEditTitle = (convId, currentTitle, location = 'sidebar') => {
    if (editingConversationId && editingConversationId !== convId) { 
        handleSaveTitle(editingConversationId, editingTitle);
    } else if (isEditingInHeader && location === 'sidebar') { 
        setIsEditingInHeader(false);
    }

    setEditingConversationId(convId);
    setEditingTitle(currentTitle);
    setIsEditingInHeader(location === 'header');
  };
  
  const handleSaveTitle = (convId, newTitle) => {
    const trimmedTitle = newTitle.trim();
    if (trimmedTitle) {
      setConversations(prevConvs =>
        prevConvs.map(conv =>
          conv.id === convId ? { ...conv, title: trimmedTitle } : conv
        )
      );
    }
    setEditingConversationId(null);
    setEditingTitle('');
    setIsEditingInHeader(false); 
  };

  const handleTitleInputChange = (e) => {
    setEditingTitle(e.target.value);
  };

  const handleTitleInputKeyDown = (e, convId) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSaveTitle(convId, editingTitle);
    } else if (e.key === 'Escape') {
      setEditingConversationId(null);
      setEditingTitle('');
      setIsEditingInHeader(false);
    }
  };

  const handleDeleteConversation = (convIdToDelete) => {
    if (editingConversationId === convIdToDelete) { 
        setEditingConversationId(null);
        setEditingTitle('');
        setIsEditingInHeader(false);
    }
    setConversations(prevConvs => {
      const deletedConvIndex = prevConvs.findIndex(c => c.id === convIdToDelete);
      const remainingConversations = prevConvs.filter(conv => conv.id !== convIdToDelete);
      
      if (remainingConversations.length === 0) {
        return []; 
      }

      if (activeConversationId === convIdToDelete) {
        let newActiveIndex = deletedConvIndex;
        if (newActiveIndex >= remainingConversations.length) {
            newActiveIndex = remainingConversations.length - 1;
        }
        if (newActiveIndex >= 0 && remainingConversations.length > 0) {
            setActiveConversationId(remainingConversations[newActiveIndex].id);
        } else if (remainingConversations.length > 0) {
            setActiveConversationId(remainingConversations[0].id);
        }
      }
      return remainingConversations;
    });
  };


  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto'; 
      inputRef.current.style.height = `${inputRef.current.scrollHeight}px`; 
    }
  }, [inputValue]);

  useEffect(() => {
    if (inputRef.current && !editingConversationId && !isEditingInHeader) { 
        inputRef.current.focus();
    }
  }, [activeConversationId, editingConversationId, isEditingInHeader]);

  useEffect(() => {
    return () => {
      if (selectedImage && selectedImage.previewUrl && selectedImage.previewUrl.startsWith('blob:')) {
        URL.revokeObjectURL(selectedImage.previewUrl);
      }
    };
  }, [selectedImage]);

  const activeConversation = conversations.find(c => c.id === activeConversationId);


  return (
    <div className="flex h-screen bg-gray-100 font-sans antialiased">
      {/* Sidebar */}
      <div className={`fixed inset-y-0 left-0 z-30 w-72 bg-white shadow-lg transform transition-transform duration-300 ease-in-out lg:static lg:translate-x-0 ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}`}>
        <div className="flex flex-col h-full">
          <div className="flex items-center justify-between p-4 border-b border-gray-200">
            <h1 className="text-xl font-semibold text-gray-800">Gemini Chat</h1>
            <button
              onClick={() => setSidebarOpen(false)}
              className="lg:hidden p-1 rounded-md text-gray-500 hover:bg-gray-100"
              aria-label="Close sidebar"
            >
              <X size={20} />
            </button>
          </div>
          <div className="p-4">
            <button
              onClick={() => startNewConversation()}
              className="w-full flex items-center justify-center gap-2.5 px-4 py-2.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50"
            >
              <Plus size={18} />
              New Conversation
            </button>
          </div>
          <nav className="flex-1 overflow-y-auto px-2 space-y-1">
            {conversations.map((conv) => (
              <div 
                key={conv.id}
                className={`group flex items-center justify-between rounded-md text-sm font-medium transition-colors ${ 
                  conv.isActive && !editingConversationId && !isEditingInHeader ? 'bg-blue-50 text-blue-700' : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
                }`}
              >
                {editingConversationId === conv.id && !isEditingInHeader ? ( 
                  <div className="flex-grow p-1">
                    <input
                      ref={titleInputRef} 
                      type="text"
                      value={editingTitle}
                      onChange={handleTitleInputChange}
                      onKeyDown={(e) => handleTitleInputKeyDown(e, conv.id)}
                      onBlur={() => handleSaveTitle(conv.id, editingTitle)}
                      className="w-full px-2 py-1.5 text-sm border border-blue-300 rounded-md focus:ring-1 focus:ring-blue-500 outline-none"
                    />
                  </div>
                ) : (
                  <a
                    href="#" 
                    onClick={(e) => { e.preventDefault(); selectConversation(conv.id); if(sidebarOpen) setSidebarOpen(false);}}
                    className="flex-grow px-3 py-2.5 truncate" 
                  >
                    <span className="truncate">{conv.title || 'Conversation'}</span>
                  </a>
                )}
                <div className="flex-shrink-0 flex items-center">
                    {editingConversationId === conv.id && !isEditingInHeader ? (
                        <button
                            onClick={() => handleSaveTitle(conv.id, editingTitle)}
                            className="p-1.5 text-green-600 hover:text-green-800 hover:bg-green-100 rounded-md"
                            aria-label="Save title"
                        >
                            <Check size={16} />
                        </button>
                    ) : (
                        !isEditingInHeader && 
                        <button
                            onClick={(e) => {
                                e.stopPropagation(); 
                                handleEditTitle(conv.id, conv.title, 'sidebar');
                            }}
                            className="p-1.5 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-md opacity-0 group-hover:opacity-100 transition-opacity"
                            aria-label="Edit title"
                        >
                            <Edit3 size={16} />
                        </button>
                    )}
                    {editingConversationId !== conv.id && !isEditingInHeader && ( 
                         <button
                            onClick={(e) => {
                                e.stopPropagation();
                                handleDeleteConversation(conv.id);
                            }}
                            className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-md opacity-0 group-hover:opacity-100 transition-opacity"
                            aria-label="Delete conversation"
                        >
                            <Trash2 size={16} />
                        </button>
                    )}
                </div>
              </div>
            ))}
          </nav>
          <div className="p-4 mt-auto border-t border-gray-200">
            <button className="w-full flex items-center justify-center gap-2.5 px-4 py-2.5 text-sm text-gray-600 hover:bg-gray-100 hover:text-gray-900 rounded-md transition-colors">
              <Settings size={18} />
              Settings
            </button>
          </div>
        </div>
      </div>

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-gray-50">
        {/* Header */}
        <header className="bg-white border-b border-gray-200 px-4 sm:px-6 py-3 shadow-sm">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3 flex-grow min-w-0"> 
              <button
                onClick={() => setSidebarOpen(true)}
                className="lg:hidden p-2 rounded-md text-gray-500 hover:bg-gray-100"
                aria-label="Open sidebar"
              >
                <Menu size={24} />
              </button>
              <div className="flex items-center gap-2 flex-grow min-w-0"> 
                <div className="w-9 h-9 bg-gradient-to-br from-blue-500 to-purple-600 rounded-full flex items-center justify-center shadow flex-shrink-0">
                  <Bot size={18} className="text-white" />
                </div>
                {isEditingInHeader && activeConversationId ? (
                    <input
                        ref={headerTitleInputRef}
                        type="text"
                        value={editingTitle}
                        onChange={handleTitleInputChange}
                        onKeyDown={(e) => handleTitleInputKeyDown(e, activeConversationId)}
                        onBlur={() => handleSaveTitle(activeConversationId, editingTitle)}
                        className="text-lg font-semibold text-gray-800 border border-blue-300 rounded-md px-2 py-0.5 focus:ring-1 focus:ring-blue-500 outline-none flex-grow min-w-0" 
                    />
                ) : (
                    <h2 
                        className="text-lg font-semibold text-gray-800 truncate cursor-pointer hover:text-blue-600 transition-colors flex-grow min-w-0" 
                        title={activeConversation?.title || 'AI Assistant'}
                        onClick={() => {
                            if (activeConversation && !isEditingInHeader) { 
                                handleEditTitle(activeConversation.id, activeConversation.title, 'header');
                            }
                        }}
                    > 
                        {activeConversation?.title || (conversations.length > 0 ? conversations[0].title : 'AI Assistant')}
                    </h2>
                )}
              </div>
            </div>
            <button
              onClick={() => startNewConversation()}
              className="p-2 rounded-md text-gray-500 hover:text-blue-600 hover:bg-blue-50 transition-colors ml-2 flex-shrink-0" 
              title="Start new conversation" 
              aria-label="Start new conversation"
            >
              <MessageSquarePlus size={20} />
            </button>
          </div>
        </header>

        {/* Messages */}
        <main className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-4">
          {messages.map((message) => (
            <div
              key={message.id}
              className={`flex gap-2.5 items-end ${message.type === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              {message.type === 'bot' && (
                <div className="w-8 h-8 bg-gradient-to-br from-blue-500 to-purple-600 rounded-full flex items-center justify-center flex-shrink-0 self-start shadow">
                  <Bot size={16} className="text-white" />
                </div>
              )}
              <div className={`max-w-xl ${message.type === 'user' ? 'order-1' : ''}`}>
                <div
                  className={`px-4 py-2.5 rounded-xl shadow-sm ${
                    message.type === 'user'
                      ? 'bg-blue-600 text-white rounded-br-none'
                      : 'bg-white text-gray-800 border border-gray-200 rounded-bl-none'
                  }`}
                >
                  {message.imagePreviewUrl && (
                    <img 
                        src={message.imagePreviewUrl} 
                        alt="User upload" 
                        className="max-w-xs max-h-64 rounded-md mb-2 border border-gray-300" 
                    />
                  )}
                  {message.content && ( 
                    <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
                        {message.content}
                    </p>
                  )}
                </div>
                <div className={`text-xs text-gray-500 mt-1.5 px-1 ${message.type === 'user' ? 'text-right' : 'text-left'}`}>
                  {formatTime(message.timestamp)}
                </div>
              </div>
              {message.type === 'user' && (
                <div className="w-8 h-8 bg-gray-300 rounded-full flex items-center justify-center flex-shrink-0 self-start shadow order-2">
                  <User size={16} className="text-gray-600" />
                </div>
              )}
            </div>
          ))}
          {isLoading && (
            <div className="flex gap-2.5 justify-start items-end">
              <div className="w-8 h-8 bg-gradient-to-br from-blue-500 to-purple-600 rounded-full flex items-center justify-center flex-shrink-0 self-start shadow">
                <Bot size={16} className="text-white" />
              </div>
              <div className="max-w-xs">
                <div className="px-4 py-3 rounded-xl bg-white shadow-sm border border-gray-200 rounded-bl-none">
                  <div className="flex items-center gap-1.5">
                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }}></div>
                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }}></div>
                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }}></div>
                  </div>
                </div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </main>

        {/* Input Area */}
        <footer className="bg-white border-t border-gray-200 p-4 sm:p-6">
          <div className="max-w-4xl mx-auto">
            {selectedImage && (
              <div className="mb-2 flex items-center justify-center p-2 bg-gray-100 rounded-lg border border-gray-200">
                <img src={selectedImage.previewUrl} alt="Selected preview" className="max-h-24 max-w-xs rounded mr-2" />
                <button onClick={removeSelectedImage} className="p-1 text-gray-500 hover:text-red-600">
                  <XCircle size={20} />
                </button>
              </div>
            )}
            <div className="flex items-end gap-2 sm:gap-3 bg-gray-100 rounded-xl p-2 sm:p-3 border border-gray-200 focus-within:ring-2 focus-within:ring-blue-500 transition-shadow">
              <input 
                type="file" 
                ref={imageInputRef} 
                onChange={handleImageFileChange} 
                accept="image/png, image/jpeg, image/webp, image/heic, image/heif" 
                className="hidden" 
              />
              <button 
                onClick={() => imageInputRef.current.click()}
                className="p-2 text-gray-500 hover:text-blue-600 rounded-lg hover:bg-gray-200 transition-colors" 
                aria-label="Attach image"
                title="Attach image"
              >
                <Paperclip size={20} /> 
              </button>
              {/* Microphone button removed */}
              <textarea
                ref={inputRef}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyPress={handleKeyPress}
                placeholder={selectedImage ? "Add a caption... (optional)" : "Type a message..."}
                className="flex-1 bg-transparent border-none outline-none resize-none text-sm text-gray-900 placeholder-gray-500 py-2 px-1 min-h-[24px] max-h-36"
                rows={1}
              />
              <button
                onClick={handleSendMessage}
                disabled={(!inputValue.trim() && !selectedImage) || isLoading}
                className={`p-2.5 rounded-lg transition-all text-white ${
                  (inputValue.trim() || selectedImage) && !isLoading
                    ? 'bg-blue-600 hover:bg-blue-700 shadow-sm'
                    : 'bg-blue-300 cursor-not-allowed'
                }`}
                aria-label="Send message"
              >
                <Send size={20} />
              </button>
            </div>
            <div className="text-center mt-2.5">
              <p className="text-xs text-gray-500">
                AI responses may be inaccurate. Verify important information.
              </p>
            </div>
          </div>
        </footer>
      </div>

      {/* Overlay for mobile sidebar */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black bg-opacity-60 z-20 lg:hidden"
          onClick={() => {
            if (editingConversationId) { 
                handleSaveTitle(editingConversationId, editingTitle);
            }
            setSidebarOpen(false);
          }}
          aria-hidden="true"
        />
      )}
    </div>
  );
};

export default AppleChatbot;
